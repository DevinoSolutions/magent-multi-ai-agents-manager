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
from typing import TYPE_CHECKING, NoReturn

import click

from magent.cli.app import main
from magent.cli.session_picker import _node_session_targets
from magent.style import style

if TYPE_CHECKING:
    from magent.nodes import Node

# `magent send` exit codes (documented in the command help + README).
_EXIT_OK = 0
_EXIT_NOT_FOUND = 2
_EXIT_PSMUX_ERROR = 3
_EXIT_NOT_CONFIRMED = 4

_EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]


def _cloud_ids(config_path: str | None) -> set[str]:
    """Session ids of this config's cloud panes (first row per id decides --
    see ``psmux.cloud_pane_ids``)."""
    from magent import psmux  # heavy subsystem: in-body per policy

    return psmux.cloud_pane_ids(psmux.config_sessions(config_path))


def _live_names(
    config_path: str | None, psmux_bin: str, *, drivable: bool = True
) -> list[str]:
    """Live psmux session names for this config, in config order.

    ``drivable`` (the default) leaves a cloud pane out: it is a viewer onto a
    cloud session, not an agent that reads typed input, so a command that
    drives panes must never reach it. A read-only one passes False."""
    from magent import psmux  # heavy subsystem: in-body per policy

    rows = psmux.config_sessions(config_path)
    cloud = psmux.cloud_pane_ids(rows) if drivable else set()
    candidates = [
        sid for sid in (psmux.socket_id(d) for d in rows) if sid and sid not in cloud
    ]
    if not candidates:
        return []
    return psmux.live_sessions(candidates, psmux=psmux_bin)


def _live_cloud_names(config_path: str | None, psmux_bin: str) -> list[str]:
    """The cloud panes that are live, for a command that drives every pane and
    must say which it left out. Disjoint from ``_live_names``' drivable set by
    construction (that one drops exactly these ids), so the two sweeps cannot
    disagree about a pane."""
    from magent import psmux  # heavy subsystem: in-body per policy

    ids = sorted(_cloud_ids(config_path))
    return psmux.live_sessions(ids, psmux=psmux_bin) if ids else []


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


def _no_match_exit(session: str, live: list[str]) -> NoReturn:
    """``session`` named nothing: print the live set and exit 2."""
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
    config_path: str | None, session: str, *, drives: str | None = None
) -> tuple[str, Node | None]:
    """Resolve ``session`` among this PC's live psmux sessions AND the node
    sessions: ``(name, node)``, ``node`` set when the match runs on a node.
    A name both carry is the local one. A missing psmux is fatal (exit 3)
    only when no node session matches; no match at all exits 2, listing
    both kinds.

    ``drives`` names the command about to TYPE into the pane (``send``,
    ``model``); a cloud pane is then refused by name (exit 2). A read-only
    command (``peek``) leaves it None. The name is judged among EVERYTHING the
    user could mean -- the cloud panes too, live or not -- and only a cloud
    WINNER is refused: resolving among cloud panes alone would refuse the
    local pane ``api`` because ``api-cloud`` contains it, and filtering the
    cloud panes out first would quietly pick a local pane for a prefix the
    cloud one shares."""
    from magent import fleet, psmux  # heavy subsystem: in-body per policy

    remote = _node_sessions(config_path)
    psmux_bin = psmux.find_psmux() if remote else _require_psmux()
    local = _live_names(config_path, psmux_bin, drivable=False) if psmux_bin else []
    names = [*local, *(n for n in remote if n not in local)]
    cloud = _cloud_ids(config_path) if drives else set()
    name = fleet.resolve_session(
        session, [*names, *(c for c in sorted(cloud) if c not in names)]
    )
    if name is None:
        if psmux_bin is None:
            _require_psmux()
        _no_match_exit(session, names)
    if name in remote and name not in local:
        return name, remote[name]
    if drives and name in cloud:
        _refuse_cloud(name, drives)
    return name, None


# What a cloud pane does with typed input, and the one thing the CLI offers
# instead. `claude -p ... --cloud <session-id>` is valid ONLY as a follow-up to
# an existing session id (spec V5), never a way to start one.
_CLOUD_FOLLOW_UP = 'claude -p "<msg>" --cloud <session-id>'


def _refuse_cloud(name: str, command: str = "send") -> NoReturn:
    """``send``/``model`` against a cloud pane: said plainly, exit 2.

    A cloud pane is only a viewer onto a cloud session, so what is typed into
    it goes to a shell -- and the one command that does work there, `claude
    --cloud`, starts a NEW billed cloud session each time (spec 18.5)."""
    # The id is the user's to supply: magent never sees a cloud session's id
    # (the CLI cannot list them), so this names the form, not where to find it.
    follow_up = (
        f" Outside magent, `{_CLOUD_FOLLOW_UP}` continues an existing cloud"
        " session, given its id (magent does not track it)."
        if command == "send"
        else ""
    )
    click.echo(
        f"  {style('x', fg='red')} {name} is a cloud session: `magent {command}` "
        "types nothing into a cloud pane, which only views the session -- a "
        f"`claude --cloud` typed there would start (and bill) another one.{follow_up}"
        f" `magent peek {name}` reads the pane.",
        err=True,
    )
    sys.exit(_EXIT_NOT_FOUND)


def _refuse_node(name: str, node: Node, command: str) -> NoReturn:
    """``send``/``model`` against a node session: said plainly, exit 2 --
    never "no live session matches", which it is not."""
    click.echo(
        f"  {style('x', fg='red')} {name} runs on node {node.nick}: "
        f"`magent {command}` is not supported for node sessions yet "
        f"(`magent peek {name}` is).",
        err=True,
    )
    sys.exit(_EXIT_NOT_FOUND)


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

    Exit codes: 0 sent, 2 session not found (or a node session: not
    supported yet; or a cloud session: it takes no typed input), 3 psmux
    error, 4 send not confirmed (the pane could not be read back, or the
    session never went idle).
    """
    from pathlib import Path

    from magent import fleet, psmux  # heavy subsystem: in-body per policy

    name, node = _resolve_target(ctx.obj.get("config_path"), session, drives="send")
    if node is not None:
        _refuse_node(name, node, "send")
    psmux_bin = _require_psmux()

    body = Path(file).read_text(encoding="utf-8") if file else (text or "")

    if compact:
        if not fleet.paste_and_enter(name, "/compact", psmux_bin=psmux_bin):
            click.echo(f"  {style('x', fg='red')} /compact send failed.", err=True)
            sys.exit(_EXIT_PSMUX_ERROR)
        idle = fleet.wait_for_idle(
            name,
            psmux_bin=psmux_bin,
            deadline=time.monotonic() + timeout,
            # The /compact was pasted a moment ago and has not taken effect yet,
            # so the pane is still idle -- believing that reading pastes the
            # prompt into a session about to start compacting. See ``settle``.
            settle=fleet.COMMAND_SETTLE_S,
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
    capture = psmux.read_pane(name, psmux=psmux_bin)
    if capture.timed_out:
        # An unread pane confirms nothing. Read as "", it passed the check
        # below -- a prompt still sitting unsent reported "OK sent".
        click.echo(
            f"  {style('!', fg='yellow')} {_unread_pane(name)}; delivery "
            f"unconfirmed. check: magent peek {name}",
            err=True,
        )
        sys.exit(_EXIT_NOT_CONFIRMED)
    if fleet.looks_unsent(capture.text, body):
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
    A per-session table is printed at the end. A node session is refused
    (exit 2): not supported yet; so is a cloud session (it takes no typed
    input). ``--all`` covers this PC's sessions, cloud panes left out (and counted).
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

    skipped: list[str] = []
    if all_:
        psmux_bin = _require_psmux()
        targets = _live_names(ctx.obj.get("config_path"), psmux_bin)
        skipped = _live_cloud_names(ctx.obj.get("config_path"), psmux_bin)
    else:
        name, node = _resolve_target(
            ctx.obj.get("config_path"), session or "", drives="model"
        )
        if node is not None:
            _refuse_node(name, node, "model")
        psmux_bin = _require_psmux()
        targets = [name]

    if skipped:
        click.echo(
            f"  {style('-', dim=True)} skipped {len(skipped)} cloud pane(s): "
            f"{', '.join(skipped)} (a cloud pane takes no typed input)"
        )
    if not targets:
        if skipped:
            click.echo(
                f"  {style('No drivable sessions.', dim=True)} The only live panes "
                "are cloud panes."
            )
        else:
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
    """Print the last LINES of a session's pane -- a read-only glance.

    A node session's pane is read on its node, with one bounded ssh call.
    Exit codes: 0 printed, 2 session not found, 3 the pane could not be read
    (psmux, or the node, did not answer)."""
    # heavy subsystem: in-body per policy
    from magent import psmux, remote_mux

    name, node = _resolve_target(ctx.obj.get("config_path"), session)
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
