"""Reconnecting SSH supervisor for attach panes (the ``magent-attach-client``
console script).

WHAT RUNS IN THE PANE. ``magent attach <host>`` opens one Windows Terminal
window per remote psmux session. Until this module existed each pane ran a bare
``ssh -t <target> "psmux -L <sid> attach || magent sessions <sid>"``, and the
first time the connection died -- laptop sleep, wi-fi change, VPN flap, host
reboot -- ssh printed ``client_loop: send disconnect: ...`` and exited 255. wt
keeps a pane open after its process exits, so every window became a
``[process exited with code 255]`` corpse and the only cure was closing forty
terminals by hand and re-running ``magent attach``.

This runs *between* wt and ssh instead: it execs the same ssh, with the same
options and the same remote command, then reads the exit code and reconnects
when the answer is "the transport died". Nothing about the interactive
experience changes -- the child inherits this process's console handles
directly (no pipes, no PTY emulation), so colors, mouse reporting and terminal
resize reach ssh exactly as before. While a connection is up this process does
nothing at all but wait on the child.

WHY ITS OWN CONSOLE SCRIPT rather than a ``magent`` subcommand: the same reason
``state_hook.py`` is one (see pyproject ``[project.scripts]``). A 40-window
attach starts 40 of these, and booting the click CLI in each -- the
registration hub imports every command module, then a config load follows -- is
exactly the cost that once made a big attach take minutes. Imports here are
stdlib plus ``click`` (already a base dependency, and only for echo/styling).

CORPSE COHERENCE -- read this before changing the argv. ``cli/attach.py``
decides a pane is dead by scanning live process command lines for
``-L <sid> attach`` (``_attach_markers``) among ``_CLIENT_PROCESS_NAMES``.
During a backoff sleep there is no ssh process at all, so this supervisor is
what has to carry the marker -- and it does, for free, because
``_spawn_windows`` hands us the remote command it would otherwise have given
ssh, as our own ``--remote`` argument. The marker therefore appears verbatim in
this process's command line. Do NOT "simplify" that by rebuilding the remote
command from ``--session`` and dropping the argument: the pane would read as a
corpse the moment it started backing off, and the next ``magent attach`` would
close a window that was busy healing itself.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

import click

from magent.style import style

# The console-script name, so cli/attach.py resolves the binary and names the
# process to scan for from one place instead of two drifting literals.
CLIENT_EXE_NAME = "magent-attach-client"

# Connection posture for the INTERACTIVE attach connection (`_ssh_capture`'s
# short non-interactive queries are separately timeout-bounded and untouched).
# Two halves, both load-bearing for the reconnect loop below.
#
# KEEPALIVE. OpenSSH sends nothing on an idle session by default, so after a
# laptop sleep or a network change the TCP connection under an attach window is
# dead while ssh.exe keeps running -- blocked forever on a socket that will
# never answer. Without these options that zombie never exits, so the
# supervisor never gets an exit code to react to and the pane stays frozen.
# 15s x 4 makes the client give up at most ~60s after the connection dies,
# which is what turns an invisible half-open socket into the exit 255 the
# reconnect loop is built to heal.
#
# CONNECT TIMEOUT. Without it a dial at a host that is off, asleep, or behind a
# black-holing network hangs in connect() for the OS default (minutes on
# Windows) -- so "reconnecting in 4s" would be a lie, and a host that came back
# during that hang would not be noticed until the kernel gave up. 20s is
# deliberately looser than `_ssh_capture`'s 10s: that one is a snappy control
# query the user is waiting on, this one only decides how fast a pane notices a
# host is still down. Any reachable host completes TCP connect in well under a
# second, tailnet or not.
#
# Losing the ssh client never loses work: the psmux session lives on the HOST
# and is untouched by the client dying, so the reattach lands on the same
# running agent, scrollback and all.
SSH_CONNECTION_OPTS = (
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
    "-o",
    "ConnectTimeout=20",
)

# OpenSSH reserves 255 for its own failures (connect refused, DNS, host
# unreachable, auth, and a mid-session `client_loop: send disconnect`). Any
# other non-zero code came from the REMOTE command, which means the connection
# itself worked -- a distinction the whole decision table below rests on.
SSH_TRANSPORT_RC = 255
SSH_MISSING_RC = 127

# Backoff: 2s doubling, capped. The cap is what makes "retry forever" safe --
# a host that is down for eight hours costs at most one handshake every 30s,
# which no sshd notices, while a blip is healed in about two seconds.
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 30.0
# Guard for the pathological caller: 2.0 * 2**1024 is an OverflowError, and a
# supervisor must not crash its own pane doing arithmetic.
_BACKOFF_EXP_CAP = 20

# A connection that stayed up this long was a real working session, so the drop
# that ended it is a fresh event and the next reconnect starts from the bottom
# of the ladder again. Without this, an all-day pane that flaps once an hour
# would eventually be sitting at the 30s cap for a blip it could heal in two.
ESTABLISHED_S = 30.0

# What one ssh exit means, as the loop reads it.
RECONNECT = "reconnect"
DETACHED = "detached"
REMOTE_FAILED = "remote-failed"


def remote_attach_command(sid: str) -> str:
    """The remote command an attach pane runs, for session ``sid``.

    Direct ``psmux attach`` first: it connects in well under a second, where
    booting the full magent CLI per window (python import + config load, x40
    windows on a loaded host) made a big attach take many minutes. The session
    picker is only the fallback, for a session id the host no longer has.

    Single-sourced here because ``cli/attach.py::_attach_markers`` has to
    recognize this exact spelling in a live process's command line -- the two
    drifting apart would make every healthy pane read as a corpse.
    """
    return f"psmux -L {sid} attach || magent sessions {sid}"


def ssh_argv(target: str, remote: str) -> list[str]:
    """The interactive ssh invocation an attach pane runs."""
    return ["ssh", *SSH_CONNECTION_OPTS, "-t", target, remote]


def verdict(rc: int) -> str:
    """What one ssh exit code means for the pane.

    Three outcomes, and the middle one is the whole feature:

    * ``0`` -- the remote command finished cleanly. The user detached
      (``psmux detach``) or quit the picker, and reconnecting would fight them.
    * ``255`` -- ssh itself failed: refused, unreachable, DNS, auth, or a
      mid-session disconnect. The session on the host is untouched, so this is
      the case worth retrying, forever.
    * anything else -- the CONNECTION worked and the remote command exited
      non-zero (no such psmux session after a host reboot, picker error, ...).
      Retrying re-runs a command that just failed for a reason a retry cannot
      change, so the loop stops and says why rather than hot-looping on sshd.

    The catch-all also swallows a ssh child that was KILLED rather than exited
    -- ``STATUS_CONTROL_C_EXIT`` / a harness timeout on Windows, a negative
    ``-signum`` from ``subprocess`` on POSIX. Landing those in "stop" is the
    safe direction: something deliberately killed the client, and a supervisor
    that answered a kill by dialling again would be unstoppable.
    """
    if rc == 0:
        return DETACHED
    if rc == SSH_TRANSPORT_RC:
        return RECONNECT
    return REMOTE_FAILED


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before reconnect ``attempt`` (1-based): 2, 4, 8, 16, 30,
    30, ... Pure, so the ladder is pinned by a unit test rather than by staring
    at a laptop for five minutes."""
    steps = min(max(attempt, 1) - 1, _BACKOFF_EXP_CAP)
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2.0**steps))


def next_attempt(attempt: int, elapsed: float) -> int:
    """The attempt counter after a connection that lasted ``elapsed`` seconds.

    Pure counterpart to ``backoff_delay``: a connection that survived
    ``ESTABLISHED_S`` resets the ladder, a short-lived one climbs it.
    """
    return 1 if elapsed >= ESTABLISHED_S else attempt + 1


def _echo(text: str) -> None:
    """One status line into the pane, flushed immediately.

    ASCII-only markers on purpose: this text can render inside a terminal whose
    encoding we do not control.

    The flush is not decoration. Python block-buffers stdout whenever it is not
    a tty, and this process then spends most of its life asleep in a backoff --
    so a redirected or piped pane (a captured log, a test harness, anything but
    a live console) would hold "reconnecting in 30s" in a buffer for minutes,
    and lose it entirely if the pane were killed. A status line the user is
    supposed to act on must never be sitting in a buffer.
    """
    click.echo(text)
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()


def _run_ssh(argv: list[str]) -> int:
    """Run ssh with this process's console INHERITED, and return its exit code.

    No pipes and no capture, deliberately: ssh must own the real console so the
    interactive session keeps its colors, mouse reporting and resize handling.
    This process is a waiter, never a middleman.
    """
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        return SSH_MISSING_RC


def supervise(target: str, remote: str, session: str, *, reconnect: bool = True) -> int:
    """Run the attach connection, reconnecting until told to stop.

    Returns the exit code the pane should carry. ``reconnect=False`` reproduces
    the historical bare-ssh behavior exactly (one connection, whatever code it
    exits with) for ``magent attach --no-reconnect``.
    """
    if shutil.which("ssh") is None:
        _echo(f"  {style('x', fg='red')} ssh is not on PATH -- cannot attach.")
        return SSH_MISSING_RC

    argv = ssh_argv(target, remote)
    attempt = 0
    while True:
        started = time.monotonic()
        rc = _run_ssh(argv)
        elapsed = time.monotonic() - started
        outcome = verdict(rc)
        if outcome == DETACHED:
            _echo(
                f"\n  {style('+', fg='green')} detached from {style(session, bold=True)}."
            )
            return 0
        if outcome == REMOTE_FAILED:
            _echo(
                f"\n  {style('x', fg='red')} {target} answered, but"
                f" {style(session, bold=True)} could not be attached"
                f" {style(f'(exit {rc})', dim=True)}."
            )
            _echo(
                f"  {style('The session may be gone on the host. Run', dim=True)}"
                f" {style('magent attach', bold=True)}"
                f" {style('to bring it back.', dim=True)}"
            )
            return rc
        if not reconnect:
            _echo(
                f"\n  {style('x', fg='red')} connection to {target} lost"
                f" {style(f'(ssh exit {rc})', dim=True)}"
                f" {style('-- auto-reconnect is off (--no-reconnect).', dim=True)}"
            )
            return rc

        attempt = next_attempt(attempt, elapsed)
        delay = backoff_delay(attempt)
        _echo(
            f"\n  {style('~', fg='yellow')}"
            f" {style(f'connection to {target} lost', fg='yellow')}"
            f" {style(f'(ssh exit {rc})', dim=True)}"
            f" {style(f'-- reconnecting in {delay:.0f}s', fg='yellow')}"
            f" {style(f'(attempt {attempt}; Ctrl+C to stop)', dim=True)}"
        )
        time.sleep(delay)
        _echo(
            f"  {style('o', fg='cyan')} reconnecting to {style(target, bold=True)}..."
        )


class Options(NamedTuple):
    """One parsed ``magent-attach-client`` invocation, already defaulted."""

    target: str
    session: str
    remote: str
    reconnect: bool


def parse_args(args: list[str]) -> Options:
    # argparse is imported in-body on purpose: cli/attach.py imports this
    # SSH_CONNECTION_OPTS / remote_attach_command, and the cli registration hub
    # imports every command module eagerly -- so a module-level argparse would
    # put its ~4ms on the critical path of `magent --help`, which never parses
    # an attach-client argv. Only the console-script entry point needs it.
    import argparse

    parser = argparse.ArgumentParser(
        prog=CLIENT_EXE_NAME,
        description=(
            "Hold one magent attach pane open across SSH disconnects. Spawned by "
            "`magent attach`; you never need to run this by hand."
        ),
    )
    parser.add_argument("--target", required=True, help="SSH target (user@host)")
    parser.add_argument("--session", required=True, help="psmux session id")
    parser.add_argument(
        "--remote",
        default=None,
        help="Remote command to run (default: attach to --session)",
    )
    parser.add_argument(
        "--no-reconnect",
        dest="reconnect",
        action="store_false",
        help="Exit when the connection drops instead of reconnecting",
    )
    ns = parser.parse_args(args)
    session = str(ns.session)
    return Options(
        target=str(ns.target),
        session=session,
        # A caller that omits --remote gets the same command, but then its own
        # argv carries no attach marker -- see the corpse-coherence note.
        remote=str(ns.remote) if ns.remote else remote_attach_command(session),
        reconnect=bool(ns.reconnect),
    )


def main(argv: list[str] | None = None) -> int:
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return supervise(
            opts.target, opts.remote, opts.session, reconnect=opts.reconnect
        )
    except KeyboardInterrupt:
        # Ctrl+C during a backoff sleep is the documented way out of a pane
        # whose host is never coming back. A traceback would bury the message.
        _echo(f"\n  {style('Stopped.', dim=True)}")
        return 130
