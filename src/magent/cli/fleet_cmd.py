"""`magent send` / `magent model` / `magent peek` -- an "API-ish" way to drive
one running agent, or the whole fleet, from another shell.

Thin shells over :mod:`magent.fleet` (the parsing + psmux choreography) and
:mod:`magent.psmux` (the one owner of every psmux subprocess). The shells own
only the exit codes and the on-screen table, per the house rule that
``sys.exit`` decisions live in ``cli/`` and subsystems return data.

Slash commands (``/model``, ``/effort``, ``/compact``) are built inside Python
and pasted through ``send-keys -l`` as a list argv -- never a shell -- so Git
Bash / MSYS can never rewrite a leading ``/`` into a Windows path.
"""

from __future__ import annotations

import sys
import time

import click

from magent.cli.app import main
from magent.style import style

# `magent send` exit codes (documented in the command help + README).
_EXIT_OK = 0
_EXIT_NOT_FOUND = 2
_EXIT_PSMUX_ERROR = 3
_EXIT_NOT_CONFIRMED = 4

_EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]


def _live_names(config_path: str | None, psmux_bin: str) -> list[str]:
    """Live psmux session names for this config, in config order."""
    from magent import psmux  # heavy subsystem: in-body per policy

    candidates = [
        sid
        for sid in (psmux.socket_id(d) for d in psmux.config_sessions(config_path))
        if sid
    ]
    if not candidates:
        return []
    return psmux.live_sessions(candidates, psmux=psmux_bin)


def _require_psmux() -> str:
    """Resolve the psmux binary or exit 3 -- there is nothing to talk to
    without it."""
    from magent import psmux  # heavy subsystem: in-body per policy

    binary = psmux.find_psmux()
    if binary:
        return binary
    click.echo(
        f"  {style('x', fg='red')} psmux not found on PATH. Install: choco install psmux",
        err=True,
    )
    sys.exit(_EXIT_PSMUX_ERROR)


def _resolve_or_exit(session: str, live: list[str]) -> str:
    """Resolve ``session`` among live names, or print the live set and exit 2."""
    from magent import fleet  # heavy subsystem: in-body per policy

    name = fleet.resolve_session(session, live)
    if name:
        return name
    click.echo(
        f"  {style('x', fg='red')} no live session matches '{session}'.", err=True
    )
    if live:
        click.echo(f"  {style('live:', dim=True)} {', '.join(live)}", err=True)
    else:
        click.echo(
            f"  {style('No live sessions.', dim=True)} Run {style('magent up', bold=True)} first.",
            err=True,
        )
    sys.exit(_EXIT_NOT_FOUND)


def _footer(state: dict[str, object]) -> str:
    """Render a parsed ``{model, effort}`` as ASCII ``model / effort``."""
    model = state.get("model") or "?"
    effort = state.get("effort") or "?"
    return f"{model} / {effort}"


@main.command("send")
@click.argument("session")
@click.argument("text", required=False)
@click.option(
    "--file",
    "file",
    type=click.Path(exists=True, dir_okay=False),
    help="Read the prompt text from a file instead of the TEXT argument.",
)
@click.option(
    "--wait-idle",
    is_flag=True,
    help="Wait until the agent is idle (not mid-turn) before sending.",
)
@click.option(
    "--compact",
    is_flag=True,
    help="Send /compact first and wait for idle, then send the prompt.",
)
@click.option(
    "--timeout",
    type=float,
    default=180.0,
    show_default=True,
    help="Seconds to wait for the session to go idle (--wait-idle / --compact).",
)
@click.pass_context
def send_cmd(
    ctx: click.Context,
    session: str,
    text: str | None,
    file: str | None,
    wait_idle: bool,
    compact: bool,
    timeout: float,
) -> None:
    """Deliver a prompt to one running agent by name.

    Resolves SESSION case-insensitively (exact, then unique substring/prefix),
    refuses if it is not live, pastes the text literally and presses Enter,
    then confirms the prompt left the input line.

    Exit codes: 0 sent, 2 session not found, 3 psmux error, 4 send not
    confirmed (or the session never went idle).
    """
    from pathlib import Path

    from magent import fleet, psmux  # heavy subsystem: in-body per policy

    psmux_bin = _require_psmux()
    name = _resolve_or_exit(session, _live_names(ctx.obj.get("config_path"), psmux_bin))

    body = Path(file).read_text(encoding="utf-8") if file else (text or "")

    if compact:
        if not fleet.paste_and_enter(name, "/compact", psmux_bin=psmux_bin):
            click.echo(f"  {style('x', fg='red')} /compact send failed.", err=True)
            sys.exit(_EXIT_PSMUX_ERROR)
        idle = fleet.wait_for_idle(
            name, psmux_bin=psmux_bin, deadline=time.monotonic() + timeout
        )
        click.echo(f"  /compact sent to {style(name, bold=True)}; idle={idle}")
        if not body.strip():
            sys.exit(_EXIT_OK if idle else _EXIT_NOT_CONFIRMED)
        if not idle:
            click.echo(
                f"  {style('!', fg='yellow')} still busy after /compact; prompt not sent.",
                err=True,
            )
            sys.exit(_EXIT_NOT_CONFIRMED)
    elif wait_idle and not fleet.wait_for_idle(
        name, psmux_bin=psmux_bin, deadline=time.monotonic() + timeout
    ):
        click.echo(
            f"  {style('!', fg='yellow')} {name} did not go idle within {timeout:g}s.",
            err=True,
        )
        sys.exit(_EXIT_NOT_CONFIRMED)

    if not body.strip():
        raise click.UsageError("no prompt text (pass TEXT, --file, or --compact alone)")

    if not fleet.paste_and_enter(name, body, psmux_bin=psmux_bin):
        click.echo(f"  {style('x', fg='red')} psmux send failed for {name}.", err=True)
        sys.exit(_EXIT_PSMUX_ERROR)

    time.sleep(1.5)
    pane = psmux.capture_pane(name, psmux=psmux_bin)
    if fleet.looks_unsent(pane, body):
        click.echo(
            f"  {style('!', fg='yellow')} prompt may still be unsent in {name}; "
            f"check: magent peek {name}",
            err=True,
        )
        sys.exit(_EXIT_NOT_CONFIRMED)

    click.echo(
        f"  {style('OK', fg='green')} sent to {style(name, bold=True)} ({len(body)} chars)"
    )


@main.command("model")
@click.argument("session", required=False)
@click.argument("model", required=False)
@click.option("--all", "all_", is_flag=True, help="Target every live session.")
@click.option(
    "--effort",
    type=click.Choice(_EFFORT_CHOICES),
    default=None,
    help="Also set the reasoning effort.",
)
@click.option(
    "--max-minutes",
    type=float,
    default=20.0,
    show_default=True,
    help="Keep retrying busy sessions for up to this long.",
)
@click.option(
    "--poll",
    type=float,
    default=15.0,
    show_default=True,
    help="Seconds between sweeps of the still-busy sessions.",
)
@click.pass_context
def model_cmd(
    ctx: click.Context,
    session: str | None,
    model: str | None,
    all_: bool,
    effort: str | None,
    max_minutes: float,
    poll: float,
) -> None:
    """Switch a session's model (and optionally effort), only while it is idle.

    Usage: ``magent model <session> <model> [--effort E]`` or
    ``magent model --all <model> [--effort E]``. Busy sessions are retried
    until --max-minutes runs out; the footer is re-read to confirm each switch.
    A per-session table is printed at the end.
    """
    from magent import fleet, psmux  # heavy subsystem: in-body per policy

    if all_:
        model = model or session
        session = None
        if not model:
            raise click.UsageError("usage: magent model --all <model> [--effort E]")
    elif not session or not model:
        raise click.UsageError(
            "usage: magent model <session> <model> [--effort E]  (or --all <model>)"
        )

    psmux_bin = _require_psmux()
    live = _live_names(ctx.obj.get("config_path"), psmux_bin)
    targets = live if all_ else [_resolve_or_exit(session or "", live)]

    if not targets:
        click.echo(f"  {style('No live sessions.', dim=True)} Run magent up first.")
        return

    rows: dict[str, dict[str, str]] = {}
    for name in targets:
        rows[name] = {
            "before": _footer(fleet.read_state(name, psmux_bin=psmux_bin)),
            "after": "-",
            "result": "pending",
        }

    pending = list(targets)
    attempts: dict[str, int] = dict.fromkeys(targets, 0)
    deadline = time.monotonic() + max_minutes * 60
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            if fleet.read_state(name, psmux_bin=psmux_bin)["state"] != "idle":
                continue
            ok = fleet.switch_model(name, model or "", effort, psmux_bin=psmux_bin)
            time.sleep(1.0)
            pane = psmux.capture_pane(name, psmux=psmux_bin)
            fmodel, feffort = fleet.parse_footer(pane)
            rows[name]["after"] = _footer({"model": fmodel, "effort": feffort})
            if ok and fleet.verify_switch(pane, model or "", effort):
                rows[name]["result"] = "ok"
                pending.remove(name)
            else:
                attempts[name] += 1
                rows[name]["result"] = f"retry {attempts[name]}"
                if attempts[name] >= 3:
                    rows[name]["result"] = "failed"
                    pending.remove(name)
        if pending and time.monotonic() < deadline:
            time.sleep(poll)

    for name in pending:
        rows[name]["result"] = "timeout"

    _print_model_table(rows)
    if any(r["result"] != "ok" for r in rows.values()):
        sys.exit(_EXIT_NOT_CONFIRMED)


def _print_model_table(rows: dict[str, dict[str, str]]) -> None:
    width = max((len(n) for n in rows), default=7)
    click.echo()
    click.echo(
        f"  {style('session'.ljust(width), bold=True)}  "
        f"{style('before'.ljust(18), bold=True)}  "
        f"{style('after'.ljust(18), bold=True)}  {style('result', bold=True)}"
    )
    for name, row in rows.items():
        colour = {"ok": "green", "failed": "red", "timeout": "yellow"}.get(
            row["result"].split()[0]
        )
        result = style(row["result"], fg=colour) if colour else row["result"]
        click.echo(
            f"  {name.ljust(width)}  {row['before']:<18}  {row['after']:<18}  {result}"
        )


@main.command("peek")
@click.argument("session")
@click.option(
    "-n",
    "--lines",
    "lines",
    type=int,
    default=40,
    show_default=True,
    help="How many trailing pane lines to print.",
)
@click.pass_context
def peek_cmd(ctx: click.Context, session: str, lines: int) -> None:
    """Print the last LINES of a session's pane -- a read-only glance."""
    from magent import psmux  # heavy subsystem: in-body per policy

    psmux_bin = _require_psmux()
    name = _resolve_or_exit(session, _live_names(ctx.obj.get("config_path"), psmux_bin))
    pane = psmux.capture_pane(name, psmux=psmux_bin)
    tail = "\n".join(pane.rstrip().splitlines()[-max(1, lines) :])
    click.echo(tail)
