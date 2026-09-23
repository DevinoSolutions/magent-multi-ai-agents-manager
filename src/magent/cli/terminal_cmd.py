"""`magent terminal` -- install the Windows Terminal keybindings that keep
Ctrl+Backspace and Shift+Enter working inside a psmux pane.

Shaped after `magent hooks`: `install` merges idempotently into the app's own
settings file, `status` reports what is wired without writing. The heritage is
Claude Code's `/terminal-setup`, which installs the Shift+Enter half -- but it
REFUSES to run inside a tmux/psmux pane, which is exactly where magent users
live, so magent has to ship the equivalent itself (and the Ctrl+Backspace half
`/terminal-setup` never had). Why these two bytes, and why the fix belongs in
the terminal rather than the multiplexer: see ``magent.wt_keys``.
"""

from __future__ import annotations

from pathlib import Path

import click

from magent import wt_keys
from magent.cli.app import main
from magent.style import style

_MARKS = {
    wt_keys.INSTALLED: ("=", "green"),
    wt_keys.ADDED: ("+", "green"),
    wt_keys.MISSING: ("x", "yellow"),
    wt_keys.CONFLICT: ("!", "yellow"),
}

REPAIR_HINT = "magent terminal install"

_NOT_WINDOWS = (
    "Windows Terminal keybindings are a Windows-only feature -- this OS has no "
    "settings.json to edit."
)


def _supported() -> bool:
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    return get_platform().supports_wt_keybindings()


def _resolve(settings_file: Path | None) -> Path | None:
    """The settings.json to act on. An explicit --settings-file always wins
    (it is the seam tests and smoke runs use); otherwise probe the three
    install layouts. None means "Windows Terminal not found"."""
    if settings_file is not None:
        return settings_file
    return wt_keys.find_settings()


def _echo_outcome(outcome: wt_keys.KeyState) -> None:
    mark, color = _MARKS[outcome.state]
    click.echo(
        f"  {style(mark, fg=color, bold=True)} {outcome.keys:<16} "
        f"{style(outcome.detail, dim=outcome.state == wt_keys.INSTALLED)}"
    )


def _echo_manual_snippet(path: Path, reason: str) -> None:
    """Refusal, with everything needed to finish the job by hand."""
    click.echo(f"  {style('x', fg='red')} Cannot edit {path}: {reason}", err=True)
    click.echo(
        f"  {style('Windows Terminal accepts comments and trailing commas; the', dim=True)}"
    )
    click.echo(
        f"  {style('stdlib JSON parser does not, and magent never rewrites a file', dim=True)}"
    )
    click.echo(f"  {style('it could not read. Add this by hand instead:', dim=True)}")
    click.echo()
    for line in wt_keys.manual_snippet().splitlines():
        click.echo(f"    {style(line, fg='cyan')}")


@main.group("terminal")
def terminal_group() -> None:
    """Keyboard fixes for the terminal your psmux sessions run in."""


@terminal_group.command("install")
@click.option(
    "--settings-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Windows Terminal settings.json to edit (default: auto-detected).",
)
def terminal_install_cmd(settings_file: Path | None) -> None:
    """Bind Ctrl+Backspace and Shift+Enter so they survive psmux.

    psmux drops key modifiers in transit, so Ctrl+Backspace arrives as a bare
    Backspace and Shift+Enter submits instead of inserting a newline. These
    Windows Terminal `sendInput` bindings resolve the chord to bytes BEFORE
    psmux sees it (the same device Claude Code's `/terminal-setup` uses -- and
    that refuses to run inside a psmux pane).

    Idempotent, and never clobbers: a key you have already bound to something
    else is reported and left alone. A timestamped backup lands beside the
    file before any write.
    """
    if not _supported():
        click.echo(f"  {style('x', fg='red')} {_NOT_WINDOWS}")
        raise SystemExit(1)
    path = _resolve(settings_file)
    if path is None:
        click.echo(f"  {style('x', fg='red')} {wt_keys.WT_NOT_FOUND_MESSAGE}", err=True)
        raise SystemExit(1)
    try:
        report = wt_keys.install(path)
    except wt_keys.SettingsParseError as exc:
        _echo_manual_snippet(path, str(exc))
        raise SystemExit(1) from exc
    except OSError as exc:
        click.echo(f"  {style('x', fg='red')} Cannot edit {path}: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(
        f"  {style(str(path), dim=True)} {style(f'({report.schema})', dim=True)}"
    )
    for outcome in report.outcomes:
        _echo_outcome(outcome)
    click.echo()
    if report.backup is not None:
        click.echo(
            f"  {style('Backup:', bold=True)} {style(str(report.backup), dim=True)}"
        )
        click.echo(
            f"  {style('Restart Windows Terminal (or reload settings) to pick them up.', dim=True)}"
        )
    else:
        click.echo(f"  {style('Nothing to change -- already installed.', dim=True)}")


@terminal_group.command("status")
@click.option(
    "--settings-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Windows Terminal settings.json to inspect (default: auto-detected).",
)
def terminal_status_cmd(settings_file: Path | None) -> None:
    """Show whether the psmux-safe keybindings are installed. Writes nothing."""
    if not _supported():
        click.echo(f"  {style('-', dim=True)} {_NOT_WINDOWS}")
        return
    path = _resolve(settings_file)
    if path is None:
        click.echo(f"  {style('x', fg='red')} {wt_keys.WT_NOT_FOUND_MESSAGE}")
        return
    click.echo(f"  {style(str(path), dim=True)}")
    try:
        doc = wt_keys.load_settings(path)
    except wt_keys.SettingsParseError as exc:
        click.echo(f"  {style('!', fg='yellow', bold=True)} unreadable: {exc}")
        click.echo(
            f"  {style('Windows Terminal allows JSONC; magent will not rewrite a', dim=True)}"
        )
        click.echo(
            f"  {style('file it cannot parse. Run `magent terminal install` for the', dim=True)}"
        )
        click.echo(f"  {style('snippet to paste by hand.', dim=True)}")
        return
    except OSError as exc:
        click.echo(f"  {style('!', fg='yellow', bold=True)} unreadable: {exc}")
        return
    outcomes = wt_keys.states(doc)
    click.echo(f"  {style(f'({wt_keys.detect_schema(doc)} schema)', dim=True)}")
    for outcome in outcomes:
        _echo_outcome(outcome)
    if any(o.state != wt_keys.INSTALLED for o in outcomes):
        click.echo()
        click.echo(f"  {style('Repair:', bold=True)} {style(REPAIR_HINT, fg='cyan')}")
