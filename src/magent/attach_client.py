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
options and the same remote command, then decides whether to dial again.
Nothing about the interactive experience changes -- the child inherits this
process's console handles directly (no pipes, no PTY emulation), so colors,
mouse reporting and terminal resize reach ssh exactly as before. While a
connection is up this process does nothing at all but wait on the child.

WHAT STOPS A PANE. The default answer is "almost nothing": a user on flaky
wi-fi wants the window to keep trying until the host comes back, not to close
because one packet went missing. The pane therefore reconnects on every exit
EXCEPT a proven deliberate detach, and "proven" is the load-bearing word --
exit 0 does not prove it. Windows OpenSSH does not propagate a remote command's
exit status over a pty, so a host-side session that DIED under the pane also
reports 0, indistinguishable from ``psmux detach``. Deciding on rc alone is
what once made every wi-fi flap close forty windows and announce them as
detaches. The truth is fetched instead: after a disconnect the supervisor asks
the host, over a separate non-pty connection where remote exit codes ARE
truthful, whether the session is still alive (``session_probe_argv``). Alive
means the user left on purpose and the pane stops; anything else means keep
trying. See ``verdict`` for the whole table.

WHY ITS OWN CONSOLE SCRIPT rather than a ``magent`` subcommand: the same reason
``state_hook.py`` is one (see pyproject ``[project.scripts]``). A 40-window
attach starts 40 of these, and booting the click CLI in each -- the
registration hub imports every command module, then a config load follows -- is
exactly the cost that once made a big attach take minutes. Imports here are
stdlib plus ``click`` (already a base dependency, and only for echo/styling).

WHAT AN OUTAGE LOOKS LIKE. Loudly, once: a pane on flaky wi-fi used to print
three lines per attempt -- ours announcing the drop, ours announcing the
redial, and ssh's own ``connect to host ... timed out`` -- so a ten-minute
outage scrolled thirty lines of the same news past the user's work. The pane
now renders ONE line, rewritten in place, that carries every changing number
(attempt, countdown, last error). ``status_text`` composes it and truncates it
to the terminal width, because a status line that wraps cannot be rewritten by
a carriage return and degenerates into exactly the scroll it replaces. See
``_StderrPump`` for what happens to ssh's own noise, and ``_stdout_is_tty`` for
the piped/redirected fallback.

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
from typing import TYPE_CHECKING, NamedTuple

import click

from magent.style import style

if TYPE_CHECKING:
    from typing import IO

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
# Windows) -- so "retry in 4s" would be a lie, and a host that came back
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
# The connection worked and the SESSION is not on the host. Worth a bounded
# number of redials (a host mid-reboot, or a `magent up` about to recreate it),
# never an unbounded one -- see SESSION_MISSING_MAX.
SESSION_MISSING = "session-missing"

# What the post-disconnect liveness probe learned about the session. This is
# the DELIBERATE-DETACH SIGNAL, and it is out-of-band on purpose: the obvious
# in-band design (have the remote command echo a sentinel and scan the pane's
# output for it) would require this process to sit between ssh and the console,
# and `_run_ssh`'s whole contract is that it never does -- piping would cost the
# session its colors, mouse reporting and resize handling.
SESSION_ALIVE = "session-alive"
SESSION_GONE = "session-gone"
PROBE_FAILED = "probe-failed"

# The probe runs WITHOUT `-t`. That is the entire trick: Windows OpenSSH does
# not propagate a remote command's exit status over a pty (see `verdict`), but
# it does over a plain non-pty channel, on every OS. So the pane keeps its
# interactive `-t` connection and the truth about the session is fetched over a
# second, one-shot, non-interactive one.
#
# BatchMode: never prompt. A probe that stopped to ask for a password would
# hang a pane that is trying to heal itself.
SESSION_PROBE_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
)
# Hard ceiling on the probe, belt to ConnectTimeout's braces: a half-open
# socket can leave ssh blocked long after connect() succeeded, and this runs on
# the path between a dropped pane and its reconnect message.
SESSION_PROBE_TIMEOUT_S = 20.0

# Consecutive "connected fine, session isn't there" answers before the pane
# stops and says so. Non-zero because a host that is rebooting, or a `magent
# up` that is still working through a 45-session bring-up, genuinely does
# answer "gone" for a minute and then "alive". Bounded because a session the
# user really did `magent down` is never coming back on its own, and the ladder
# would otherwise dial that host every 30s forever.
SESSION_MISSING_MAX = 5

# ---------------------------------------------------------------------------
# Presentation. Everything below draws the outage; nothing below decides it.
# ---------------------------------------------------------------------------

# Rewrite-in-place control sequence: carriage return, then erase the whole line.
# Only ever written when stdout is a tty (see `_stdout_is_tty`).
ERASE_LINE = "\r\x1b[2K"

# How long the "last: ..." clause may get before it is elided. Sized so the
# longest real ssh diagnostic that matters ("Connection timed out",
# "Host key verification failed", "Permission denied (publickey,password)")
# survives whole, and a pathological multi-hundred-character line does not.
LAST_ERROR_MAX = 64

# How many of the failed dial's stderr lines are kept for the stop paths. The
# pane swallows ssh's noise while it is healing itself, so a pane that GIVES UP
# has to be able to hand back what ssh actually said -- 12 lines covers the
# whole host-key-changed block, which is the longest thing ssh emits.
STDERR_TAIL_LINES = 12

# The one thing the pump refuses to swallow. A changed host key is a security
# event, not connection noise, and the pane may never reach a stop path that
# would flush the tail (a 255 loop redials forever). Safe to print mid-outage
# by construction: host-key verification fails during the handshake, so there
# is no live remote session for the write to land in.
STDERR_ALWAYS_SHOW = ("REMOTE HOST IDENTIFICATION HAS CHANGED",)

# Ceiling on waiting for the stderr reader to finish after ssh exits. The pipe
# is at EOF by then so the thread returns immediately; this only exists so a
# wedged reader can never wedge the pane.
STDERR_JOIN_S = 2.0

# The countdown repaints at this cadence. 1s is under the threshold where a
# static number reads as a hung pane, and costs one ~100-byte write per second
# during an outage.
TICK_S = 1.0

# Narrowest width the status line is composed for. Below this a terminal cannot
# show anything useful anyway, and the clipping math stops being meaningful.
MIN_WIDTH = 20


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


def session_probe_argv(target: str, session: str) -> list[str]:
    """The one-shot, non-interactive question "is ``session`` still alive?".

    ``psmux``, not ``magent``: the answer must not depend on the host having a
    magent new enough to have grown a probe subcommand, and psmux is by
    definition installed on any host that has sessions to attach to. Old host,
    new client works; the reverse is unaffected because an old client simply
    never runs this.

    The explicit ``-t <session>`` is REQUIRED and is the same lesson
    ``psmux.has_session`` documents: a BARE ``has-session`` exits 0 for a socket
    with no server at all (psmux keeps ``__warm__`` spare servers per socket),
    which here would report every dead session as a deliberate detach and close
    the pane -- precisely the bug being fixed.

    A host where ``psmux`` is not on the SSH session's PATH answers
    "command not found" (9009 on cmd, 127 on a POSIX shell), i.e. non-zero, i.e.
    NOT ``SESSION_ALIVE`` -- so the pane keeps trying rather than closing. Only
    a positive, unambiguous rc 0 is allowed to stop a pane.
    """
    return [
        "ssh",
        *SESSION_PROBE_OPTS,
        target,
        f"psmux -L {session} has-session -t {session}",
    ]


def _probe_session(target: str, session: str) -> str:
    """Ask the host whether ``session`` is still there. Never raises."""
    try:
        rc = subprocess.run(
            session_probe_argv(target, session),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=SESSION_PROBE_TIMEOUT_S,
            check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        # No ssh, a timeout, anything unexpected: we learned nothing. The
        # caller reads that as "keep trying", which is the direction the user
        # asked for on a flaky link.
        return PROBE_FAILED
    if rc == 0:
        return SESSION_ALIVE
    if rc == SSH_TRANSPORT_RC:
        # ssh's own failure, not the remote command's: the host is unreachable
        # right now, so the session's fate is simply unknown.
        return PROBE_FAILED
    return SESSION_GONE


def verdict(rc: int, probe: str | None = None) -> str:
    """What one ssh exit means for the pane.

    ``rc`` alone is not enough, and that is the bug this signature exists to
    fix. **Windows OpenSSH does not propagate a remote command's exit status
    over a pty session** (measured; see the real-ssh tier's
    ``test_a_failing_remote_command_stops_instead_of_hammering_sshd``):
    ``ssh -t win-host "exit 7"`` reports 0 where POSIX sshd reports 7. A magent
    host is usually Windows -- psmux is Windows-native -- so when a host-side
    session died under the pane, ssh handed back 0 and the old table read that
    as "the user detached", closed the pane, and told them so. On a flaky link
    that turned every wi-fi flap into forty closed windows.

    So exit 0 is no longer trusted to mean "deliberate detach". ``probe`` is,
    because it is fetched over a NON-pty connection where the remote status is
    truthful on every OS (see ``session_probe_argv``).

    The table:

    * ``rc == 255`` -- ssh's own failure: refused, unreachable, DNS, auth, or a
      mid-session ``client_loop: send disconnect``. Produced by the LOCAL ssh
      client, so it is trustworthy everywhere and needs no probe. The session on
      the host is untouched; retry, forever. This is the flaky-wi-fi path and it
      is deliberately the one branch that costs no extra round-trip.
    * probe says the session is ALIVE -- the client left while the work kept
      running: a real ``psmux detach``, a quit picker, or a killed ssh child.
      Stop; reconnecting would fight the user.
    * probe says the session is GONE -- the connection worked and the thing to
      attach to is not there. Retry, but counted: see ``SESSION_MISSING_MAX``.
    * probe learned nothing -- reconnect. On a link that is dropping, "I could
      not ask" is far likelier to mean "the network is still bad" than "the user
      detached", and closing the pane on that guess is what this fixes.

    ``probe=None`` is the ``--no-reconnect`` path only, and keeps the historical
    rc-only table byte for byte: that flag's entire promise is that the pane
    behaves exactly as a bare ssh did.
    """
    if rc == SSH_TRANSPORT_RC:
        return RECONNECT
    if probe is None:
        return DETACHED if rc == 0 else REMOTE_FAILED
    if probe == SESSION_ALIVE:
        return DETACHED
    if probe == SESSION_GONE:
        return SESSION_MISSING
    return RECONNECT


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
    """One PERMANENT line into the pane, flushed immediately.

    Everything that scrolls goes through here; everything that is rewritten in
    place goes through ``StatusLine`` instead. The split is the whole feature:
    an outage now leaves at most one permanent line behind it, not three per
    attempt.

    ASCII-only markers on purpose: this text can render inside a terminal whose
    encoding we do not control.

    The flush is not decoration. Python block-buffers stdout whenever it is not
    a tty, and this process then spends most of its life asleep in a backoff --
    so a redirected or piped pane (a captured log, a test harness, anything but
    a live console) would hold "retry in 30s" in a buffer for minutes, and lose
    it entirely if the pane were killed. A status line the user is supposed to
    act on must never be sitting in a buffer.
    """
    click.echo(text)
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()


def _write(text: str) -> None:
    """Raw, unterminated write to the pane. Used only for in-place repaints.

    ``color=True`` is not a style choice, it is the whole mechanism: click
    strips every ``\\x1b[...]`` sequence from a stream it does not consider a
    color sink, and ``ERASE_LINE`` is such a sequence. Stripped, the repaint
    becomes a bare carriage return that overwrites only the first N characters
    of the previous line and leaves its tail behind -- the exact wrap-garbage
    this is meant to remove. Only ever reached when ``_stdout_is_tty`` already
    said yes, so forcing the sequences through is honest.
    """
    click.echo(text, nl=False, color=True)
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()


def _stdout_is_tty() -> bool:
    """Whether the pane can be repainted in place.

    Checked ONCE per supervisor, not per repaint: a redirected pane (a captured
    log, a test harness, ``magent attach ... > file``) must get one plain line
    per attempt instead of a stream of carriage returns and erase-line escapes,
    which in a log file are unreadable garbage rather than an animation.
    """
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _term_width() -> int:
    """Columns available for the status line, re-read on every repaint.

    One column is held back deliberately. Writing exactly ``columns``
    characters puts the cursor past the last cell, which on the Windows console
    scrolls a line -- and a status line that scrolls is a status line the next
    carriage return can no longer reach.
    """
    try:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    except (OSError, ValueError):
        columns = 80
    return max(MIN_WIDTH, columns - 1)


def _clip(text: str, width: int) -> str:
    """``text`` shortened to at most ``width`` printable cells."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def condense_error(line: str) -> str:
    """One short, single-line, ASCII-safe summary of an ssh diagnostic.

    Three jobs, all of them about fitting inside a status line that must stay
    exactly one terminal row:

    * ``ssh: connect to host box port 22: Connection timed out`` keeps only the
      part that varies ("Connection timed out"). The prefix is the same on
      every failure and would crowd out the countdown.
    * control characters and non-ASCII are flattened, so ``len()`` is the real
      rendered width -- the truncation math depends on that, and a stray
      newline from a chatty sshd would break the line into two.
    * a pathological length is elided rather than allowed to wrap.
    """
    ascii_only = line.encode("ascii", "replace").decode("ascii")
    text = " ".join(ascii_only.split())
    if text.startswith("ssh: "):
        _head, sep, tail = text.rpartition(": ")
        if sep and tail:
            text = tail
    return _clip(text.rstrip("."), LAST_ERROR_MAX)


def status_text(
    *,
    target: str,
    attempt: int,
    remaining: float | None = None,
    last_error: str = "",
    width: int = 80,
) -> str:
    """The whole outage as one plain line, already clipped to ``width``.

    Pure, and separate from the writer, so the thing that has to be exactly one
    row wide is provable without a terminal: a line that exceeds the width
    wraps, and a wrapped line cannot be rewritten by a carriage return -- the
    next repaint then lands on the wrap remnant and the pane fills with the
    garbage this whole change exists to remove.

    ``remaining=None`` means the countdown is over and ssh is dialing right
    now; the line stays up during the dial because ssh writes nothing to the
    console until the remote session paints over it.

    WHAT GETS SACRIFICED, NARROWEST FIRST, and the order is a judgement about
    a TILED pane -- magent's whole point is forty windows across the monitors,
    so 60-column panes are the normal case, not the edge one:

    * the ``Ctrl+C to stop`` hint goes first. It never changes, and a user who
      has read it once does not need it on every repaint.
    * the target goes next. It is already in the window title (magent owns
      those), so the pane is not ambiguous without it -- whereas the reason is
      genuinely new information the user has nowhere else.
    * the attempt number and the countdown are last, because they are what say
      "this pane is alive and will try again", which is the entire message.
    """
    when = ", dialing" if remaining is None else f", retry in {remaining:.0f}s"
    reason = condense_error(last_error) if last_error else ""
    wide = f"~ reconnecting to {target} (attempt {attempt}{when}"
    narrow = f"~ reconnecting (attempt {attempt}{when}"
    hint = ") -- Ctrl+C to stop"
    said = f", last: {reason}" if reason else ""
    for candidate in (
        f"{wide}{said}{hint}",
        f"{wide}{said})",
        f"{narrow}{said}{hint}",
        f"{narrow}{said})",
        f"{wide}{hint}",
        f"{wide})",
        f"{narrow}{hint}",
        f"{narrow})",
    ):
        if len(candidate) <= width:
            return candidate
    # Nothing fits whole. Keep the numbers, elide the rest.
    return _clip(f"{narrow})", width)


def _duration(seconds: float) -> str:
    """Compact, ASCII-only run length for the reconnected record."""
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


class StatusLine:
    """The single pane row an outage owns, rewritten in place.

    Not a spinner class for its own sake: it exists so that exactly one place
    knows whether a status line is currently on screen. Erasing a line that was
    never drawn would blank whatever the remote session left behind, and
    forgetting to erase one leaves ``reconnecting ... retry in 1s`` frozen
    above the restored session forever.

    ``tty=False`` makes every method a no-op; the caller then prints one plain
    line per attempt instead.
    """

    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self._shown = False

    def show(self, text: str) -> None:
        if not self._tty:
            return
        _write(ERASE_LINE + style(text, fg="yellow"))
        self._shown = True

    def clear(self) -> None:
        if self._tty and self._shown:
            _write(ERASE_LINE)
            self._shown = False


class _StderrPump:
    """Reads a failed dial's stderr off a pipe so it never scrolls the pane.

    WHY CAPTURING ssh's STDERR IS SAFE, and this is the reasoning to re-check
    before widening it. The noise the user reported -- ``ssh: connect to host
    ... Connection timed out``, ``client_loop: send disconnect``, ``Connection
    closed by <ip>`` -- is written by the ssh CHILD, not by us, so no amount of
    in-place repainting on our side can stop it scrolling. The only way to
    quiet it is to give ssh a pipe for fd 2.

    That is safe here for two independent reasons:

    * **Interactive prompts do not use stderr.** OpenSSH asks for passwords,
      passphrases and host-key confirmations through ``read_passphrase()``,
      which opens the controlling terminal directly (``/dev/tty`` on POSIX, the
      console on the Windows port) precisely so prompts survive redirection.
      Piping fd 2 therefore cannot swallow a prompt or the answer to one.
    * **A live session's output does not use stderr either.** The connection is
      made with ``-t``, so the remote command's stdout AND stderr are both
      multiplexed through the pty and arrive on our STDOUT, which stays
      inherited and untouched. Local fd 2 carries only the ssh client's own
      diagnostics.

    What is left -- ssh client diagnostics -- is exactly what the status line's
    ``last: ...`` clause is for, so nothing is lost, only relocated. Two escape
    hatches keep that honest anyway: ``STDERR_ALWAYS_SHOW`` passes a changed
    host key straight through, and the tail is flushed verbatim when the pane
    gives up (see ``_dump``). Redirected panes and ``--no-reconnect`` never
    capture at all -- they keep fd 2 inherited, exactly as before.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        # In-body per this module's startup-cost policy (see `parse_args`): a
        # 40-window attach imports this module 40 times and only a pane that is
        # actually mid-outage ever needs a thread.
        import threading

        self._stream = stream
        self.last = ""
        self._tail: list[str] = []
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            for raw in self._stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.last = line
                self._tail.append(line)
                del self._tail[:-STDERR_TAIL_LINES]
                if any(marker in line for marker in STDERR_ALWAYS_SHOW):
                    click.echo(line, err=True)

    def close(self) -> tuple[str, tuple[str, ...]]:
        """Stop reading and return ``(last line, tail)``.

        The join comes first and the close second, never the other way round:
        ssh has already exited by the time this is called, so the pipe is at
        EOF and the reader returns on its own. Closing under a live reader
        would only trade a tidy exit for a suppressed exception.
        """
        self._thread.join(timeout=STDERR_JOIN_S)
        with contextlib.suppress(OSError, ValueError):
            self._stream.close()
        return self.last, tuple(self._tail)


class Dial(NamedTuple):
    """One ssh connection, as the loop sees it after the fact."""

    rc: int
    # Last line ssh wrote to stderr, or "" when stderr was left inherited.
    error: str
    # Bounded tail of the same, kept only so a pane that GIVES UP can hand back
    # what ssh actually said instead of having quietly eaten it.
    detail: tuple[str, ...]


def _run_ssh(argv: list[str], *, capture: bool = False) -> Dial:
    """Run ssh with this process's console INHERITED, and report how it went.

    stdin and stdout are never touched: ssh must own the real console so the
    interactive session keeps its colors, mouse reporting and resize handling.
    This process is a waiter, never a middleman. ``capture`` redirects fd 2
    ONLY -- see ``_StderrPump`` for why that is safe and why it is off for
    redirected panes and ``--no-reconnect``.
    """
    try:
        proc = subprocess.Popen(argv, stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError:
        return Dial(SSH_MISSING_RC, "", ())
    pump = _StderrPump(proc.stderr) if capture and proc.stderr is not None else None
    rc = proc.wait()
    if pump is None:
        return Dial(rc, "", ())
    last, tail = pump.close()
    return Dial(rc, last, tail)


def _dump(detail: tuple[str, ...]) -> None:
    """Hand back the ssh output a healing pane swallowed, when it stops trying.

    Only ever reached on a stop path: while the pane is still redialing the
    same three lines every attempt are noise, but a pane that is about to sit
    there dead owes the user the real diagnostic.
    """
    if not detail:
        return
    _echo(f"  {style('ssh said:', dim=True)}")
    for line in detail:
        _echo(f"    {style(condense_error(line), dim=True)}")


def _wait(
    status: StatusLine,
    *,
    target: str,
    attempt: int,
    delay: float,
    last_error: str,
    tty: bool,
) -> None:
    """Sleep out one backoff, saying so.

    On a tty the countdown is repainted in place once a second, so an outage
    costs one screen row no matter how long it lasts. Off a tty it is one plain
    line and one plain sleep -- carriage returns in a log file are noise, and
    the per-second repaints would be a hundred identical log lines.
    """
    if not tty:
        _echo(
            "  "
            + style(
                status_text(
                    target=target,
                    attempt=attempt,
                    remaining=delay,
                    last_error=last_error,
                    width=10_000,
                ),
                fg="yellow",
            )
        )
        time.sleep(delay)
        return
    remaining = delay
    while remaining > 0:
        status.show(
            status_text(
                target=target,
                attempt=attempt,
                remaining=remaining,
                last_error=last_error,
                width=_term_width(),
            )
        )
        step = TICK_S if remaining > TICK_S else remaining
        time.sleep(step)
        remaining -= step
    # The line stays up, now reading "dialing", for the whole of the next dial:
    # ssh prints nothing to the console until the remote session paints over
    # it, so this is the pane's only sign of life during a 20s connect timeout.
    status.show(
        status_text(
            target=target,
            attempt=attempt,
            last_error=last_error,
            width=_term_width(),
        )
    )


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
    # In-place repainting and stderr capture are one decision, taken once, and
    # both are off for `--no-reconnect` -- that flag's whole promise is the
    # historical bare-ssh pane, down to which fd ssh's errors land on.
    tty = reconnect and _stdout_is_tty()
    status = StatusLine(tty=tty)
    attempt = 0
    # Consecutive SESSION_MISSING answers. Reset by anything else, so a host
    # that flaps between "gone" and "reachable" never accumulates its way to a
    # stop -- only a session that is steadily absent does.
    missing = 0
    last_error = ""
    detail: tuple[str, ...] = ()
    try:
        while True:
            started = time.monotonic()
            dial = _run_ssh(argv, capture=tty)
            rc = dial.rc
            elapsed = time.monotonic() - started
            detail = dial.detail
            # From here on the terminal is ours again, so the outage's status
            # line -- last seen reading "dialing" -- goes before anything else
            # is printed.
            status.clear()
            if attempt and elapsed >= ESTABLISHED_S:
                # The permanent record of a healed outage, and the only line an
                # outage leaves in the scrollback. It is written HERE, at the
                # drop that ended the session, rather than the moment the
                # session came back, because at that moment ssh owns the
                # console: the remote psmux has entered the alternate screen
                # and anything we print lands inside the user's agent pane as
                # garbage no redraw will repair. This module never writes into
                # a live session -- see the class docstring's "waiter, never a
                # middleman". Scrollback order is unaffected either way: the
                # line still sits between the outage it ended and the next one.
                _echo(
                    f"  {style('+', fg='green')} reconnected to"
                    f" {style(target, bold=True)}"
                    f" {style(f'after {attempt} attempt(s); stayed up', dim=True)}"
                    f" {style(_duration(elapsed), dim=True)}."
                )
            # `--no-reconnect` asks for the historical bare-ssh pane, which
            # means no probe either: one connection, one rc, one message.
            probe = (
                None
                if not reconnect or rc == SSH_TRANSPORT_RC
                else _probe_session(target, session)
            )
            outcome = verdict(rc, probe)
            if outcome == DETACHED:
                _echo(
                    f"\n  {style('+', fg='green')} detached from"
                    f" {style(session, bold=True)}."
                )
                return 0
            if outcome == REMOTE_FAILED:
                _echo(
                    f"\n  {style('x', fg='red')} {target} answered, but"
                    f" {style(session, bold=True)} could not be attached"
                    f" {style(f'(exit {rc})', dim=True)}."
                )
                _dump(detail)
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

            if outcome == SESSION_MISSING:
                missing += 1
                if missing >= SESSION_MISSING_MAX:
                    _echo(
                        f"\n  {style('x', fg='red')} {target} is reachable, but"
                        f" {style(session, bold=True)} is not a session there"
                        f" {style(f'(checked {missing} times)', dim=True)}."
                    )
                    _dump(detail)
                    _echo(
                        f"  {style('Run', dim=True)}"
                        f" {style('magent attach', bold=True)}"
                        f" {style('to bring it back.', dim=True)}"
                    )
                    # rc is untrustworthy here by construction (a Windows host
                    # reports 0 for a remote command that failed), so a stop
                    # that means "this pane gave up" must not be able to exit 0.
                    return rc or 1
            else:
                missing = 0

            # One sentence for what went wrong, reused by every repaint of the
            # countdown. ssh's own last word wins when we captured it; the
            # fallbacks keep a redirected pane (where fd 2 is inherited and we
            # never saw it) just as informative as it was before.
            if outcome == SESSION_MISSING:
                last_error = f"{session} is not on {target} yet"
            elif dial.error:
                last_error = dial.error
            else:
                last_error = f"connection to {target} lost (ssh exit {rc})"

            attempt = next_attempt(attempt, elapsed)
            _wait(
                status,
                target=target,
                attempt=attempt,
                delay=backoff_delay(attempt),
                last_error=last_error,
                tty=tty,
            )
    except KeyboardInterrupt:
        # The status line is mid-repaint when Ctrl+C lands; leaving it there
        # would freeze "retry in 7s" above whatever prints next.
        status.clear()
        raise


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
