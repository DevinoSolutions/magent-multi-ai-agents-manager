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

from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

LOGO_LINES = [
    r"                         _   ",
    r" _ __  __ _ __ _ ___ _ _| |_ ",
    r"| '  \/ _` / _` / -_) ' \  _|",
    r"|_|_|_\__,_\__, \___|_||_\__|",
    r"           |___/",
]


def _banner() -> None:
    # deferred: resolving __version__ costs an importlib.metadata import, and
    # only the banner needs it -- the banner-free fast paths shouldn't pay it.
    from magent import __version__

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


def _edit_and_wait(path: Path) -> None:
    """Open PATH in the user's editor and BLOCK until that editor is closed.

    ``_open_in_editor``'s fire-and-forget launch is right for "here is your
    config, carry on" -- but useless for a fetch/edit/push cycle, which must
    not read the file back before the user has saved and closed it. Windows
    ``os.startfile`` in particular returns immediately. ``click.edit``
    resolves VISUAL/EDITOR (falling back to the platform default) and waits
    for the process, which is exactly the contract that flow needs.
    """
    click.edit(filename=str(path))


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


def _down_reasons(entries: list[dict[str, object]]) -> dict[str, str]:
    """Map psmux socket id -> why it is down, for the entries that carry one.

    ``reason`` is optional and additive (``psmux_status`` only sets it on the
    projects it never even probed), so an entry without one simply renders as
    the bare name it always did.
    """
    out: dict[str, str] = {}
    for e in entries:
        raw = e.get("session") or e.get("name")
        why = e.get("reason")
        if isinstance(raw, str) and raw and isinstance(why, str) and why:
            out[raw] = why
    return out


def _print_names(
    names: list[str],
    indent: str = "       ",
    width: int = 66,
    reasons: dict[str, str] | None = None,
) -> None:
    """Wrap session names across dim lines, annotating the ones with a reason.

    A named reason renders as ``eBay (folder not found)``. It is plain data
    from ``psmux_status``, never attach-specific wording, so this stays correct
    for the host-side `up`/`status` flows that share this renderer.
    """
    # `plain` carries the unstyled text the width math needs; `line` carries
    # the same content with per-token styling (ANSI would corrupt len()).
    plain = indent
    line = indent
    for nm in names:
        why = (reasons or {}).get(nm)
        token = f"{nm} ({why})" if why else nm
        if plain.strip() and len(plain) + len(token) + 2 > width:
            click.echo(line)
            plain = indent
            line = indent
        plain += token + "  "
        line += style(nm, dim=True)
        if why:
            line += " " + style(f"({why})", dim=True)
        line += "  "
    if plain.strip():
        click.echo(line)


def _print_session_overview(
    hostname: str, up: list[dict[str, object]], down: list[dict[str, object]]
) -> list[str]:
    """Render a grouped up/down overview; return the ordered list of pickable groups."""
    dn_order, dn_buckets = _grouped(down)
    up_order, up_buckets = _grouped(up)
    dn_reasons = _down_reasons(down)

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
        _print_names(names, reasons=dn_reasons)

    for g in up_order:
        if g not in dn_buckets:
            cnt = len(up_buckets[g])
            click.echo(
                f"     {style(g, dim=True)}  {style(f'{cnt}/{cnt} ready', fg='green')}"
            )
    _divider()
    return pickable
