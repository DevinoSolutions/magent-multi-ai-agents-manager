"""Pure presentation leaf: banner/menu chrome and the config-editor's
printing helpers. No config or subprocess state of its own beyond the two
platform-guarded helpers (_force_utf8_console: win-only ctypes;
_print_qr: optional `qrcode` dep) -- both keep their guard in-body.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import click

from magent import __version__
from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

LOGO_LINES = [
    r"           _ _   _    _        _   ",
    r" _ __ _  _| | |_(_)__| |___ __| |__",
    r"| '  \ || | |  _| / _` / -_) _| / /",
    r"|_|_|_\_,_|_|\__|_\__,_\___\__|_\_\\",
]


def _banner() -> None:
    click.echo()
    for line in LOGO_LINES:
        click.echo(f"  {style(line, fg='cyan')}")
    click.echo(
        f"  {style(f'v{__version__}', dim=True)}  {style('auto-tile your AI workspace', dim=True)}"
    )
    click.echo()


def _divider() -> None:
    click.echo(f"  {style('-' * 40, dim=True)}")


def _menu_item(key: str, label: str, key_fg: str = "cyan", extra: str = "") -> None:
    click.echo(f"   {style(key, fg=key_fg, bold=True)}   {label}{extra}")


def _grid_preview(cols: int, rows: int, indent: str = "  ") -> list[str]:
    cell_w = 10
    lines: list[str] = []
    border = "+" + (f"{'-' * cell_w}+") * cols
    for r in range(rows):
        lines.append(f"{indent}{style(border, dim=True)}")
        cells = ""
        for c in range(cols):
            n = r * cols + c + 1
            label = f"win {n}"
            pad = cell_w - len(label)
            left = pad // 2
            right = pad - left
            cells += (
                style("|", dim=True)
                + " " * left
                + style(label, fg="cyan")
                + " " * right
            )
        cells += style("|", dim=True)
        lines.append(f"{indent}{cells}")
    lines.append(f"{indent}{style(border, dim=True)}")
    return lines


def _open_in_editor(path: Path) -> None:
    path_str = str(path)
    if sys.platform == "win32":
        os.startfile(path_str)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path_str])
    else:
        from magent.env import editor_command  # heavy subsystem: in-body per policy

        subprocess.Popen([editor_command(), path_str])


def _confirm_change(message: str) -> None:
    click.echo(f"\n  {style('+', fg='green', bold=True)} {message}")
    click.echo(f"  {style('Press Enter to continue...', dim=True)}", nl=False)
    click.getchar()
    click.echo()


def _prompt_or_back(
    label: str, default: str = "", *, show_default: bool = True
) -> str | None:
    hint = style("  (b to go back)", dim=True)
    value: str = click.prompt(
        f"  {label}{hint}", default=default, show_default=show_default
    ).strip()
    if value.lower() == "b":
        return None
    return value


def _force_utf8_console() -> None:
    """Make stdout render UTF-8 (block chars for the QR, box glyphs) on Windows
    consoles that default to a legacy code page. Best-effort: the expected
    OS/attribute errors (redirected stdout, missing console) are suppressed so a
    cosmetic failure never crashes the CLI; an unexpected error still surfaces."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, AttributeError):
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    with contextlib.suppress(OSError, AttributeError, ValueError):
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _print_qr(url: str) -> None:
    """Print a scannable QR for the URL if the qrcode lib is available."""
    try:
        import qrcode  # ty: ignore[unresolved-import]  # reason: optional dep, guarded by try/except (see pyproject qrcode note)
    except ImportError:
        click.echo(
            f"  {style('Tip:', dim=True)} {style('pip install qrcode', bold=True)} "
            f"{style('to print a scannable QR code here.', dim=True)}"
        )
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _grouped(
    entries: list[dict[str, object]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Bucket session entries by project group, preserving first-seen order."""
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    for e in entries:
        group = e.get("group")
        g = group if isinstance(group, str) and group else "(no group)"
        if g not in buckets:
            buckets[g] = []
            order.append(g)
        # Bucket by the psmux socket id (P3-01): these buckets feed both the
        # status/overview display AND the bring-up/kill target lists, so they
        # must carry the id, not the display name.
        raw_name = e.get("session") or e.get("name")
        buckets[g].append(raw_name if isinstance(raw_name, str) else "")
    return order, buckets


def _print_names(names: list[str], indent: str = "       ", width: int = 66) -> None:
    line = indent
    for nm in names:
        if line.strip() and len(line) + len(nm) + 2 > width:
            click.echo(style(line, dim=True))
            line = indent
        line += nm + "  "
    if line.strip():
        click.echo(style(line, dim=True))


def _print_session_overview(
    hostname: str, up: list[dict[str, object]], down: list[dict[str, object]]
) -> list[str]:
    """Render a grouped up/down overview; return the ordered list of pickable groups."""
    dn_order, dn_buckets = _grouped(down)
    up_order, up_buckets = _grouped(up)

    click.echo()
    click.echo(
        f"  {style('Sessions on', bold=True)} {style(hostname, fg='cyan')}    "
        f"{style(str(len(up)), fg='green', bold=True)} up  {style('/', dim=True)}  "
        f"{style(str(len(down)), fg='yellow', bold=True)} down"
    )
    _divider()

    pickable: list[str] = []
    for g in dn_order:
        names = dn_buckets[g]
        up_n = len(up_buckets.get(g, []))
        total = up_n + len(names)
        if g == "(no group)":
            click.echo(
                f"     {style(g, dim=True)}  {style(f'{up_n}/{total}', dim=True)}"
            )
        else:
            pickable.append(g)
            num = style(str(len(pickable)), fg="cyan", bold=True)
            click.echo(
                f"  {num}  {style(g, bold=True)}  {style(f'{up_n}/{total} up', dim=True)}"
            )
        _print_names(names)

    for g in up_order:
        if g not in dn_buckets:
            cnt = len(up_buckets[g])
            click.echo(
                f"     {style(g, dim=True)}  {style(f'{cnt}/{cnt} ready', fg='green')}"
            )
    _divider()
    return pickable
