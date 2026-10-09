"""`magent send` / `model` / `peek` / `choose` / `interrupt` -- drive one
running agent, or the whole fleet, from another shell.

Thin shells over :mod:`magent.control`, the same functions the ``/api/v1``
write routes call: a shell resolves the session name (fuzzy, as it always
has), calls the leaf, maps ``ControlError`` to the exit codes below and
prints. ``--json`` prints the exact ``/api/v1`` envelope (``magent.wire``),
so a script and an HTTP client read one shape. Lint rule MD010 keeps every
keystroke in ``control``/``fleet``/``uploads``.

Slash commands (``/model``, ``/effort``, ``/compact``) are built inside Python
and pasted through ``send-keys -l`` as a list argv -- never a shell -- so Git
Bash / MSYS can never rewrite a leading ``/`` into a Windows path.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING, NoReturn

import click

from magent import wire
from magent.cli.app import main
from magent.cli.session_picker import _node_session_targets
from magent.style import style
from magent.wire import WireError

if TYPE_CHECKING:
    from magent.nodes import Node

# `magent send` exit codes (documented in the command help + README).
_EXIT_OK = 0
_EXIT_NOT_FOUND = 2
_EXIT_PSMUX_ERROR = 3
_EXIT_NOT_CONFIRMED = 4

# How a refusal from ``magent.control`` maps onto those codes. Anything not
# listed (``unavailable``, ``internal``) is a psmux-side failure.
# ``invalid_request`` lands on 2, the same code Click gives a usage error,
# because that is what it is: an argument the leaf would not take.
_EXIT_BY_CODE: dict[str, int] = {
    "not_found": _EXIT_NOT_FOUND,
    "conflict": _EXIT_NOT_FOUND,
    "invalid_request": _EXIT_NOT_FOUND,
    "timeout": _EXIT_NOT_CONFIRMED,
}

_EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]

# A usage error Click itself refuses -- ``choose`` with an option outside 1-9,
# ``send`` with no prompt text, ``model --all --json`` -- never reaches the
# leaf, so it is Click's exit 2 with its usage line on stderr and no envelope.
_JSON_HELP = (
    "Print the /api/v1 envelope ({ok, data} or {ok, error}) on stdout. "
    "A usage error still exits 2 with no envelope."
)


def _exit_code(exc: WireError) -> int:
    return _EXIT_BY_CODE.get(exc.code, _EXIT_PSMUX_ERROR)


def _emit(envelope: dict[str, object], code: int = _EXIT_OK) -> NoReturn:
    """Print one envelope on stdout and exit."""
    click.echo(json.dumps(envelope))
    sys.exit(code)


def _fail(exc: WireError, as_json: bool, text: str | None = None) -> NoReturn:
    """A refusal: the error envelope on stdout (``--json``), or one line on
    stderr -- ``text`` when the shell has its own wording -- then the mapped
    exit code."""
    if as_json:
        _emit(wire.from_exc(exc), _exit_code(exc))
    glyph = style("!", fg="yellow") if exc.code == "timeout" else style("x", fg="red")
    click.echo(text or f"  {glyph} {exc.message}.", err=True)
    sys.exit(_exit_code(exc))


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


def _require_psmux(as_json: bool = False) -> str:
    """Resolve the psmux binary or exit 3 -- there is nothing to talk to
    without it."""
    from magent import psmux  # heavy subsystem: in-body per policy

    binary = psmux.find_psmux()
    if binary:
        return binary
    _fail(
        WireError("unavailable", "psmux not found on PATH"),
        as_json,
        f"  {style('x', fg='red')} psmux not found on PATH. Install: choco install psmux",
    )


def _resolve_or_exit(session: str, live: list[str], as_json: bool = False) -> str:
    """Resolve ``session`` among live names, or print the live set and exit 2."""
    from magent import fleet  # heavy subsystem: in-body per policy

    name = fleet.resolve_session(session, live)
    if name:
        return name
    if as_json:
        _fail(
            WireError(
                "not_found", f"no live session matches '{session}'", {"live": live}
            ),
            True,
        )
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


def _node_sessions(config_path: str | None) -> dict[str, Node]:
    """The node sessions a fleet command may name: session id -> the Node it
    runs on, for every node row the sync daemon did not last see dead (live,
    or stale -- not heard from, so worth one bounded try). Reads files only;
    never dials."""
    return {
        str(row["name"]): node
        for row, node in _node_session_targets(config_path)
        if node is not None and row["state"] != "dead"
    }


def _resolve_target(
    config_path: str | None, session: str, *, as_json: bool = False
) -> tuple[str, Node | None]:
    """Resolve ``session`` among this PC's live psmux sessions AND the node
    sessions: ``(name, node)``, ``node`` set when the match runs on a node.
    A name both carry is the local one. A missing psmux is fatal (exit 3)
    only when no node session matches; no match at all exits 2, listing
    both kinds."""
    from magent import fleet, psmux  # heavy subsystem: in-body per policy

    remote = _node_sessions(config_path)
    psmux_bin = psmux.find_psmux() if remote else _require_psmux(as_json)
    local = _live_names(config_path, psmux_bin) if psmux_bin else []
    names = [*local, *(n for n in remote if n not in local)]
    name = fleet.resolve_session(session, names)
    if name is not None and name in remote and name not in local:
        return name, remote[name]
    if psmux_bin is None:
        _require_psmux(as_json)
    return _resolve_or_exit(session, names, as_json), None


def _refuse_node(
    name: str, node: Node, command: str, *, as_json: bool = False
) -> NoReturn:
    """A write verb against a node session: said plainly, exit 2 -- never
    "no live session matches", which it is not. The same ``conflict`` the
    HTTP route answers."""
    _fail(
        WireError(
            "conflict",
            f"{name} runs on node {node.nick}; it cannot be driven from this host",
            {"reason": "node"},
        ),
        as_json,
        f"  {style('x', fg='red')} {name} runs on node {node.nick}: "
        f"`magent {command}` is not supported for node sessions yet "
        f"(`magent peek {name}` is).",
    )


def _stdout_safe(text: str) -> str:
    """``text`` reduced to what THIS process's stdout can actually encode.

    A pane is the AGENT's screen, not magent's, so it carries whatever glyphs
    the agent paints: Claude Code's input caret (U+276F), the footer's middle
    dot (U+00B7), box-drawing rules. magent's own output obeys an ASCII-only
    rule (see ``psmux``'s status-bar comments); text captured from someone
    else's UI cannot.

    On Windows a REDIRECTED stdout is the legacy code page -- measured cp1252 on
    a stock box -- and echoing a real Claude Code pane through it died with
    ``UnicodeEncodeError`` and exit 1. So ``magent peek proj`` worked in a
    console and CRASHED as ``magent peek proj > tail.txt`` or ``| findstr``.
    Unencodable characters become ``?``: ``peek`` is a lossy glance by
    definition, and losing a glyph is strictly better than losing the command.
    The symmetric move to ``psmux.capture_pane``'s ``errors="replace"`` decode.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
    except LookupError:
        # An stdout naming a codec this interpreter does not have. Nothing can
        # be transcoded, and refusing to print would be the worse answer.
        return text


def _unread_pane(name: str) -> str:
    """The one wording for a capture that ran out its budget (send + peek)."""
    from magent import psmux  # heavy subsystem: in-body per policy

    return f"could not read {name}'s pane within {psmux.CAPTURE_PANE_TIMEOUT_S:g}s"


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
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@click.pass_context
def send_cmd(
    ctx: click.Context,
    session: str,
    text: str | None,
    file: str | None,
    wait_idle: bool,
    compact: bool,
    timeout: float,
    as_json: bool,
) -> None:
    """Deliver a prompt to one running agent by name.

    Resolves SESSION case-insensitively (exact, then unique substring/prefix),
    refuses if it is not live, pastes the text literally and presses Enter,
    then confirms the prompt left the input line.

    Exit codes: 0 sent, 2 session not found (a node session, not supported
    yet; or a session that died between the name lookup and the send, which
    is ``not_found`` too), 3 psmux error, 4 send not confirmed (the pane
    could not be read back, or the session never went idle).
    """
    from pathlib import Path

    from magent import control, fleet  # heavy subsystem: in-body per policy

    config_path = ctx.obj.get("config_path")
    name, node = _resolve_target(config_path, session, as_json=as_json)
    if node is not None:
        _refuse_node(name, node, "send", as_json=as_json)
    body = Path(file).read_text(encoding="utf-8") if file else (text or "")
    if not compact and not body.strip():
        raise click.UsageError("no prompt text (pass TEXT, --file, or --compact alone)")

    try:
        result = control.send(
            config_path,
            name,
            body,
            wait_idle=wait_idle,
            compact=compact,
            timeout_s=timeout,
        )
    except control.ControlError as exc:
        if compact and exc.details.get("reason") == "not_idle" and not as_json:
            click.echo(f"  /compact sent to {style(name, bold=True)}; idle=False")
        _fail(exc, as_json)

    if as_json:
        _emit(wire.ok(result), _EXIT_OK if result.confirmed else _EXIT_NOT_CONFIRMED)
    if compact:
        idle = result.confirmed if not body.strip() else True
        click.echo(f"  /compact sent to {style(name, bold=True)}; idle={idle}")
        if not body.strip():
            sys.exit(_EXIT_OK if idle else _EXIT_NOT_CONFIRMED)
    if not result.confirmed and result.pane_state_after == fleet.TIMEOUT_STATE:
        # An unread pane confirms nothing: a prompt still sitting unsent
        # must not read as "OK sent".
        click.echo(
            f"  {style('!', fg='yellow')} {_unread_pane(name)}; delivery "
            f"unconfirmed. check: magent peek {name}",
            err=True,
        )
        sys.exit(_EXIT_NOT_CONFIRMED)
    if not result.confirmed:
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
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP + " One session only.")
@click.pass_context
def model_cmd(
    ctx: click.Context,
    session: str | None,
    model: str | None,
    all_: bool,
    effort: str | None,
    max_minutes: float,
    poll: float,
    as_json: bool,
) -> None:
    """Switch a session's model (and optionally effort), only while it is idle.

    Usage: ``magent model <session> <model> [--effort E]`` or
    ``magent model --all <model> [--effort E]``. Busy sessions are retried
    until --max-minutes runs out; the footer is re-read to confirm each switch.
    A per-session table is printed at the end. A node session is refused
    (exit 2): not supported yet; ``--all`` covers this PC's sessions.
    ``--json`` (one session) prints the last attempt's ``/api/v1`` result, or
    its refusal when no attempt produced one.
    """
    from magent import control, fleet  # heavy subsystem: in-body per policy

    if all_ and as_json:
        raise click.UsageError("--json takes one session, not --all")
    if all_:
        model = model or session
        session = None
        if not model:
            raise click.UsageError("usage: magent model --all <model> [--effort E]")
    elif not session or not model:
        raise click.UsageError(
            "usage: magent model <session> <model> [--effort E]  (or --all <model>)"
        )

    config_path = ctx.obj.get("config_path")
    if all_:
        psmux_bin = _require_psmux()
        targets = _live_names(config_path, psmux_bin)
    else:
        name, node = _resolve_target(config_path, session or "", as_json=as_json)
        if node is not None:
            _refuse_node(name, node, "model", as_json=as_json)
        psmux_bin = _require_psmux(as_json)
        targets = [name]

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
    last: dict[str, control.ModelResult | control.ControlError] = {}
    deadline = time.monotonic() + max_minutes * 60
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            try:
                result = control.set_model(config_path, name, model or "", effort)
            except control.ControlError as exc:
                if exc.details.get("reason") == "busy":
                    continue  # one attempt per idle moment; try next sweep
                last[name] = exc
                if exc.code in {"not_found", "conflict", "invalid_request"}:
                    rows[name]["result"] = "failed"
                    pending.remove(name)
                    continue
                result = None
            if result is not None:
                last[name] = result
                rows[name]["after"] = _footer(
                    {"model": result.model, "effort": result.effort}
                )
            if result is not None and result.verified:
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

    if as_json:
        [name] = targets
        outcome = last.get(name)
        if isinstance(outcome, control.ControlError):
            # The refusal itself: a failed send or a bad name is not an idle
            # timeout.
            _fail(outcome, True)
        if outcome is not None:
            ok = rows[name]["result"] == "ok"
            _emit(wire.ok(outcome), _EXIT_OK if ok else _EXIT_NOT_CONFIRMED)
        _fail(
            WireError(
                "timeout",
                f"{name} did not go idle within {max_minutes:g} minutes",
                {"reason": "not_idle"},
            ),
            True,
        )
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
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@click.pass_context
def peek_cmd(ctx: click.Context, session: str, lines: int, as_json: bool) -> None:
    """Print the last LINES of a session's pane -- a read-only glance.

    A node session's pane is read on its node, with one bounded ssh call.
    Exit codes: 0 printed, 2 session not found, 3 the pane could not be read
    (psmux, or the node, did not answer)."""
    # heavy subsystem: in-body per policy
    from magent import control, psmux, remote_mux

    config_path = ctx.obj.get("config_path")
    name, node = _resolve_target(config_path, session, as_json=as_json)
    if as_json:
        try:
            result = control.read_pane(config_path, name, lines)
        except control.ControlError as exc:
            _fail(exc, True)
        _emit(wire.ok(result), _EXIT_PSMUX_ERROR if result.timed_out else _EXIT_OK)
    if node is not None:
        pane = remote_mux.capture_pane(node, name)
        if pane is None:
            click.echo(
                f"  {style('x', fg='red')} could not read {name}'s pane on node "
                f"{node.nick} (the node, or its tmux, did not answer).",
                err=True,
            )
            sys.exit(_EXIT_PSMUX_ERROR)
        text = pane
    else:
        capture = psmux.read_pane(name, psmux=_require_psmux())
        if capture.timed_out:
            click.echo(
                f"  {style('x', fg='red')} {_unread_pane(name)} (psmux did not answer).",
                err=True,
            )
            sys.exit(_EXIT_PSMUX_ERROR)
        text = capture.text
    tail = "\n".join(text.rstrip().splitlines()[-max(1, lines) :])
    click.echo(_stdout_safe(tail))


@main.command("choose")
@click.argument("session")
@click.argument("option", type=click.IntRange(1, 9))
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@click.pass_context
def choose_cmd(ctx: click.Context, session: str, option: int, as_json: bool) -> None:
    """Answer the numbered dialog on SESSION's screen with OPTION (1-9).

    The digit is pressed alone, and only while the pane shows a dialog -- a
    digit is never typed into a prompt. Exit codes: 0 chosen, 2 session not
    found, no dialog on screen, or a node session, 3 psmux error, 4 the
    dialog is still showing afterwards."""
    from magent import control  # heavy subsystem: in-body per policy

    config_path = ctx.obj.get("config_path")
    name, node = _resolve_target(config_path, session, as_json=as_json)
    if node is not None:
        _refuse_node(name, node, "choose", as_json=as_json)
    try:
        result = control.choose(config_path, name, option)
    except control.ControlError as exc:
        _fail(exc, as_json)
    code = _EXIT_OK if result.confirmed else _EXIT_NOT_CONFIRMED
    if as_json:
        _emit(wire.ok(result), code)
    if not result.confirmed:
        click.echo(
            f"  {style('!', fg='yellow')} {name} still shows a dialog after "
            f"{option}; check: magent peek {name}",
            err=True,
        )
        sys.exit(code)
    click.echo(
        f"  {style('OK', fg='green')} chose {option} in {style(name, bold=True)} "
        f"(pane: {result.pane_state_after})"
    )


@main.command("interrupt")
@click.argument("session")
@click.option("--json", "as_json", is_flag=True, help=_JSON_HELP)
@click.pass_context
def interrupt_cmd(ctx: click.Context, session: str, as_json: bool) -> None:
    """Press Escape in SESSION -- Claude Code's interrupt for a running turn.

    Exit codes: 0 pressed, 2 session not found (or a node session), 3 psmux
    error."""
    from magent import control  # heavy subsystem: in-body per policy

    config_path = ctx.obj.get("config_path")
    name, node = _resolve_target(config_path, session, as_json=as_json)
    if node is not None:
        _refuse_node(name, node, "interrupt", as_json=as_json)
    try:
        result = control.interrupt(config_path, name)
    except control.ControlError as exc:
        _fail(exc, as_json)
    if as_json:
        _emit(wire.ok(result))
    click.echo(
        f"  {style('OK', fg='green')} interrupted {style(name, bold=True)} "
        f"(pane: {result.pane_state_after})"
    )
