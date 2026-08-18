"""The reconnecting SSH supervisor each attach pane runs.

No real ssh anywhere in this file: ``_run_ssh`` is replaced by a scripted list
of exit codes, and ``time.sleep`` records instead of sleeping. That keeps the
two things worth pinning -- the exit-code decision table and the backoff ladder
-- provable in milliseconds on every OS, and it keeps the suite from ever
opening a socket.

The one contract this module shares with ``cli/attach.py`` (its command line
carries the session's attach marker, so a pane mid-reconnect never reads as a
corpse) is pinned from the attach side, where the spawn argv is actually built:
``tests/unit/test_attach.py::TestSpawnWindows``.
"""

from __future__ import annotations

import io
import os
import subprocess
from typing import NamedTuple

import pytest

from magent import attach_client


class _Completed(NamedTuple):
    """The one field ``_probe_session`` reads off ``subprocess.run``."""

    returncode: int


class TestVerdict:
    """What one ssh exit MEANS. Getting this wrong either abandons a healable
    pane (the reported bug: forty windows closed by one wi-fi flap) or
    hot-loops on someone's sshd."""

    def test_a_transport_failure_reconnects_without_even_asking_the_host(self):
        # `client_loop: send disconnect`, connection refused, host unreachable,
        # DNS, auth -- OpenSSH reserves 255 for all of its own failures, and it
        # is the LOCAL client that reports it, so it is trustworthy on every
        # OS. This is the flaky-wi-fi path: it must cost no extra round-trip.
        assert attach_client.verdict(255) == attach_client.RECONNECT
        assert (
            attach_client.verdict(255, attach_client.SESSION_GONE)
            == attach_client.RECONNECT
        )

    def test_a_live_session_is_the_only_proof_of_a_deliberate_detach(self):
        # The client left while the work kept running: `psmux detach`, a quit
        # picker, a killed ssh child. Reconnecting would fight the user.
        assert (
            attach_client.verdict(0, attach_client.SESSION_ALIVE)
            == attach_client.DETACHED
        )

    @pytest.mark.parametrize("rc", [0, 1, 130, 3221225786])
    def test_exit_zero_alone_never_closes_a_pane_again(self, rc):
        # THE REGRESSION GUARD. Windows OpenSSH reports 0 for a remote command
        # that failed over a pty, so a session killed on the host looked
        # exactly like a deliberate detach -- and the pane closed, announcing
        # a detach that never happened.
        assert (
            attach_client.verdict(rc, attach_client.SESSION_GONE)
            == attach_client.SESSION_MISSING
        )

    @pytest.mark.parametrize("rc", [0, 1, 127, -9])
    def test_an_unanswerable_probe_keeps_trying(self, rc):
        # "I could not ask the host" is far likelier to mean "the network is
        # still bad" than "the user detached"; closing the pane on that guess
        # is the whole bug.
        assert (
            attach_client.verdict(rc, attach_client.PROBE_FAILED)
            == attach_client.RECONNECT
        )

    @pytest.mark.parametrize("rc", [1, 2, 126, 127, 130, 254, -15, 3840])
    def test_without_a_probe_the_historical_table_is_preserved(self, rc):
        # probe=None is the --no-reconnect path, whose entire promise is that
        # the pane behaves exactly as a bare ssh always did.
        assert attach_client.verdict(rc) == attach_client.REMOTE_FAILED

    def test_without_a_probe_a_clean_exit_is_still_a_detach(self):
        assert attach_client.verdict(0) == attach_client.DETACHED


class TestSessionProbe:
    """The deliberate-detach signal. Out-of-band on purpose: scanning the
    pane's output for a sentinel would make this process a middleman on the
    console, and the interactive session would lose colors and resize."""

    def test_the_probe_asks_psmux_not_magent(self):
        # Old host, new client must work: psmux is installed on any host that
        # has sessions to attach to, a magent new enough to answer a probe
        # subcommand is not.
        argv = attach_client.session_probe_argv("me@box", "api")
        assert argv[0] == "ssh"
        assert argv[-2] == "me@box"
        assert argv[-1] == "psmux -L api has-session -t api"

    def test_it_drops_the_pty_so_the_remote_exit_code_is_truthful(self):
        # Windows OpenSSH loses a remote status over `-t` and keeps it without.
        # That asymmetry is the only reason this probe can answer at all.
        assert "-t" not in attach_client.session_probe_argv("me@box", "api")

    def test_it_carries_the_explicit_target_that_makes_has_session_honest(self):
        # A BARE `has-session` exits 0 for a socket with no server at all
        # (psmux keeps __warm__ spares), which here would report every dead
        # session as a deliberate detach and close the pane -- the exact bug.
        assert "-t api" in attach_client.session_probe_argv("me@box", "api")[-1]

    def test_it_never_prompts(self):
        assert "BatchMode=yes" in attach_client.session_probe_argv("me@box", "api")

    @pytest.mark.parametrize(
        ("rc", "expected"),
        [
            (0, attach_client.SESSION_ALIVE),
            (1, attach_client.SESSION_GONE),
            (9009, attach_client.SESSION_GONE),  # cmd.exe: command not found
            (127, attach_client.SESSION_GONE),  # POSIX sh: command not found
            (255, attach_client.PROBE_FAILED),  # ssh itself could not connect
        ],
    )
    def test_only_a_positive_zero_reports_the_session_alive(
        self, monkeypatch, rc, expected
    ):
        # Everything that is not an unambiguous yes keeps the pane trying. A
        # host missing psmux from its sshd PATH must not close forty windows.
        monkeypatch.setattr(
            attach_client.subprocess, "run", lambda *_a, **_k: _Completed(rc)
        )
        assert attach_client._probe_session("me@box", "api") == expected

    @pytest.mark.parametrize(
        "exc", [OSError("no ssh"), subprocess.TimeoutExpired("ssh", 20)]
    )
    def test_a_probe_that_cannot_run_learns_nothing_rather_than_crashing(
        self, monkeypatch, exc
    ):
        def boom(*_a, **_k):
            raise exc

        monkeypatch.setattr(attach_client.subprocess, "run", boom)
        assert (
            attach_client._probe_session("me@box", "api") == attach_client.PROBE_FAILED
        )


class TestBackoffLadder:
    def test_the_ladder_doubles_from_two_seconds(self):
        assert [attach_client.backoff_delay(n) for n in (1, 2, 3, 4)] == [
            2.0,
            4.0,
            8.0,
            16.0,
        ]

    def test_it_caps_so_forever_is_cheap(self):
        # "Retry forever" is only safe because the ceiling turns an all-night
        # outage into two handshakes a minute.
        assert attach_client.backoff_delay(5) == attach_client.BACKOFF_MAX_S
        assert attach_client.backoff_delay(50) == attach_client.BACKOFF_MAX_S

    def test_a_huge_attempt_count_does_not_overflow(self):
        # A pane left running for months must not crash doing 2 ** attempt.
        assert attach_client.backoff_delay(100000) == attach_client.BACKOFF_MAX_S

    def test_a_nonsense_attempt_still_yields_the_base_delay(self):
        assert attach_client.backoff_delay(0) == attach_client.BACKOFF_BASE_S


class TestAttemptCounter:
    def test_a_short_lived_connection_climbs_the_ladder(self):
        assert attach_client.next_attempt(2, elapsed=0.5) == 3

    def test_an_established_connection_resets_it(self):
        # An all-day pane that flaps once an hour would otherwise sit at the
        # 30s cap for a blip it could have healed in two seconds.
        assert attach_client.next_attempt(9, elapsed=attach_client.ESTABLISHED_S) == 1
        assert attach_client.next_attempt(9, elapsed=3600.0) == 1


def _dial(rc, error="", detail=()):
    """One scripted ssh connection, as ``_run_ssh`` reports it."""
    return attach_client.Dial(rc, error, tuple(detail))


def _drive(monkeypatch, codes, *, durations=None, probes=None, tty=False, **kwargs):
    """Run ``supervise`` against a scripted sequence of ssh exit codes.

    ``codes`` entries are plain exit codes, or ``Dial``s when a test cares what
    ssh wrote to stderr. ``tty`` decides which of the two rendering modes runs:
    the default (False) is the plain-line fallback, which is also what a real
    piped/logged pane gets, and it keeps the sleep ladder one sleep per backoff
    so the ladder assertions below stay readable.

    ``probes`` scripts what the host answers about the session after each
    disconnect; it defaults to ``SESSION_ALIVE``, which is the historical
    "exit 0 means detached" reading and keeps the older assertions honest.

    Returns ``(rc, calls, sleeps)``: the supervisor's own exit code, the argv
    of every ssh it ran, and every backoff it waited out. A sequence that runs
    dry is a test bug (the loop only ends on a stopping verdict), so it raises
    rather than hanging.
    """
    remaining = list(codes)
    clock = list(durations or [])
    answers = list(probes or [])
    calls: list[list[str]] = []
    sleeps: list[float] = []
    now = [0.0]

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if not remaining:
            raise AssertionError("supervise asked for more connections than scripted")
        now[0] += clock.pop(0) if clock else 0.0
        code = remaining.pop(0)
        return code if isinstance(code, attach_client.Dial) else _dial(code)

    def fake_probe(_target, _session):
        return answers.pop(0) if answers else attach_client.SESSION_ALIVE

    monkeypatch.setattr(attach_client.shutil, "which", lambda _n: "/usr/bin/ssh")
    monkeypatch.setattr(attach_client, "_run_ssh", fake_run)
    monkeypatch.setattr(attach_client, "_probe_session", fake_probe)
    monkeypatch.setattr(attach_client, "_stdout_is_tty", lambda: tty)
    monkeypatch.setattr(attach_client, "_term_width", lambda: 100)
    monkeypatch.setattr(attach_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(attach_client.time, "sleep", sleeps.append)
    rc = attach_client.supervise("user@host", "psmux -L api attach", "api", **kwargs)
    return rc, calls, sleeps


class TestSupervise:
    def test_a_clean_exit_stops_without_reconnecting(self, monkeypatch, capsys):
        rc, calls, sleeps = _drive(monkeypatch, [0])
        assert (rc, len(calls), sleeps) == (0, 1, [])
        assert "detached from api" in capsys.readouterr().out

    def test_a_dropped_connection_is_redialled_until_it_sticks(
        self, monkeypatch, capsys
    ):
        # The whole feature: the pane heals itself and the user does nothing.
        rc, calls, sleeps = _drive(monkeypatch, [255, 255, 255, 0])
        assert rc == 0
        assert len(calls) == 4
        assert sleeps == [2.0, 4.0, 8.0]
        out = capsys.readouterr().out
        assert out.count("connection to user@host lost") == 3
        assert "retry in 2s" in out
        assert "attempt 3" in out

    def test_every_attempt_redials_the_identical_command(self, monkeypatch):
        # A reconnect that drifted from the original would land the user in a
        # different session -- or in the picker -- after a network blip.
        _rc, calls, _sleeps = _drive(monkeypatch, [255, 0])
        assert calls[0] == calls[1]
        assert calls[0] == [
            "ssh",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "ConnectTimeout=20",
            "-t",
            "user@host",
            "psmux -L api attach",
        ]

    def test_a_long_lived_session_restarts_the_ladder_on_its_next_drop(
        self, monkeypatch
    ):
        # Two short failures climb to 4s; a connection that lasted an hour then
        # resets, so the drop after it waits the base delay again.
        _rc, _calls, sleeps = _drive(
            monkeypatch,
            [255, 255, 255, 0],
            durations=[0.1, 0.1, 3600.0, 0.0],
        )
        assert sleeps == [2.0, 4.0, 2.0]

    def test_a_session_killed_on_the_host_is_retried_not_announced_as_a_detach(
        self, monkeypatch, capsys
    ):
        # THE REPORTED BUG, end to end. A wi-fi flap killed the host's psmux
        # session; ssh handed the pane exit 0 (Windows OpenSSH loses a remote
        # status over a pty) and the pane closed saying "detached". Now the
        # host is asked, says the session is gone, and the pane waits for
        # `magent up` to bring it back instead of vanishing.
        rc, calls, sleeps = _drive(
            monkeypatch,
            [0, 0, 0],
            probes=[
                attach_client.SESSION_GONE,
                attach_client.SESSION_GONE,
                attach_client.SESSION_ALIVE,
            ],
        )
        assert rc == 0
        assert len(calls) == 3
        assert sleeps == [2.0, 4.0]
        out = capsys.readouterr().out
        assert "api is not on user@host yet" in out
        assert "detached" not in out.split("api is not on")[0]

    def test_a_session_that_stays_gone_stops_instead_of_hammering_sshd(
        self, monkeypatch, capsys
    ):
        # The other half of the same contract: the host is perfectly healthy
        # and the session is simply not coming back (`magent down` ran). A
        # supervisor that redialled forever would dial that sshd every 30s
        # until the machine was rebooted.
        rc, calls, _sleeps = _drive(
            monkeypatch,
            [0] * attach_client.SESSION_MISSING_MAX,
            probes=[attach_client.SESSION_GONE] * attach_client.SESSION_MISSING_MAX,
        )
        assert len(calls) == attach_client.SESSION_MISSING_MAX
        # Never 0: a pane that gave up must not look like a clean detach to
        # whatever reads its exit code.
        assert rc == 1
        out = capsys.readouterr().out
        assert "is not a session there" in out
        assert "magent attach" in out

    def test_a_flapping_host_never_accumulates_its_way_to_a_stop(
        self, monkeypatch, capsys
    ):
        # Only a STEADILY absent session stops a pane. Alternating answers --
        # exactly what a host mid-reboot or mid-bring-up produces -- must reset
        # the counter, or a long flaky day would close the window by attrition.
        codes = [0, 255] * attach_client.SESSION_MISSING_MAX
        # Only the non-255 dials consult the host, so there are exactly
        # SESSION_MISSING_MAX "gone" answers -- each one cleared by the 255
        # that follows it -- and then a clean detach.
        probes = [attach_client.SESSION_GONE] * attach_client.SESSION_MISSING_MAX
        rc, calls, _sleeps = _drive(
            monkeypatch,
            [*codes, 0],
            probes=[*probes, attach_client.SESSION_ALIVE],
        )
        assert (rc, len(calls)) == (0, len(codes) + 1)
        assert "is not a session there" not in capsys.readouterr().out

    def test_an_unreachable_host_is_never_probed_and_never_gives_up(self, monkeypatch):
        # The flaky-wi-fi hot path: exit 255 comes from the LOCAL ssh client,
        # is trustworthy on every OS, and must cost no extra round-trip -- so
        # a scripted probe list that would run dry proves none was consulted.
        rc, calls, sleeps = _drive(
            monkeypatch, [255] * 12 + [0], probes=[attach_client.SESSION_ALIVE]
        )
        assert (rc, len(calls)) == (0, 13)
        assert sleeps[-1] == attach_client.BACKOFF_MAX_S

    def test_no_reconnect_reports_the_drop_and_exits(self, monkeypatch, capsys):
        rc, calls, sleeps = _drive(monkeypatch, [255], reconnect=False)
        assert (rc, len(calls), sleeps) == (255, 1, [])
        assert "--no-reconnect" in capsys.readouterr().out

    def test_no_reconnect_never_probes_the_host(self, monkeypatch, capsys):
        # That flag's whole promise is the historical bare-ssh pane: one
        # connection, one exit code, no second dial of any kind.
        def never(*_a, **_k):
            raise AssertionError("--no-reconnect must not probe the host")

        monkeypatch.setattr(attach_client, "_probe_session", never)
        monkeypatch.setattr(attach_client.shutil, "which", lambda _n: "/usr/bin/ssh")
        monkeypatch.setattr(attach_client, "_run_ssh", lambda *_a, **_k: _dial(1))
        rc = attach_client.supervise(
            "user@host", "psmux -L api attach", "api", reconnect=False
        )
        assert rc == 1
        assert "could not be attached" in capsys.readouterr().out

    def test_a_missing_ssh_client_fails_fast_and_loudly(self, monkeypatch, capsys):
        monkeypatch.setattr(attach_client.shutil, "which", lambda _n: None)
        rc = attach_client.supervise("user@host", "psmux -L api attach", "api")
        assert rc == attach_client.SSH_MISSING_RC
        assert "ssh is not on PATH" in capsys.readouterr().out

    def test_a_missing_ssh_binary_mid_loop_does_not_traceback(self, monkeypatch):
        # _run_ssh translates FileNotFoundError into an exit code so the pane
        # shows a message instead of a stack trace.
        monkeypatch.setattr(
            attach_client.subprocess,
            "Popen",
            lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError()),
        )
        assert attach_client._run_ssh(["ssh"]).rc == attach_client.SSH_MISSING_RC


class TestStatusText:
    """The one line an outage owns. Pure and tested apart from the writer on
    purpose: what has to be provable is the TEXT (it fits one row, it says what
    changed), not the escape sequences that paint it."""

    def test_it_says_everything_that_changes_in_one_line(self):
        line = attach_client.status_text(
            target="amind@amin-desktop",
            attempt=3,
            remaining=8.0,
            last_error="ssh: connect to host amin-desktop port 22: Connection timed out",
            width=120,
        )
        assert "\n" not in line
        assert "reconnecting to amind@amin-desktop" in line
        assert "attempt 3" in line
        assert "retry in 8s" in line
        assert "last: Connection timed out" in line
        assert "Ctrl+C to stop" in line

    @pytest.mark.parametrize("width", [20, 32, 47, 60, 79, 80, 100, 200])
    def test_it_never_exceeds_the_width_it_was_given(self, width):
        # THE LOAD-BEARING PROPERTY. A status line wider than the terminal
        # wraps, and a wrapped line cannot be rewritten by a carriage return --
        # the next repaint lands on the remnant and the pane fills with exactly
        # the garbage this feature removes.
        line = attach_client.status_text(
            target="a-very-long-user@a-very-long-hostname.example.internal",
            attempt=17,
            remaining=30.0,
            last_error="ssh: connect to host x port 22: Connection timed out",
            width=width,
        )
        assert len(line) <= width

    def test_a_tiled_pane_keeps_the_reason_and_drops_the_hint_then_the_target(
        self,
    ):
        # Narrow panes are the NORMAL case here -- forty windows tiled across
        # the monitors is the product. The fixed "Ctrl+C to stop" hint goes
        # first (it never changes), then the target (it is in the window title
        # already); the reason survives both because it is the only genuinely
        # new information on the line.
        kwargs = {
            "attempt": 4,
            "remaining": 16.0,
            "last_error": "Connection timed out",
        }
        line = attach_client.status_text(
            target="amind@amin-desktop", width=72, **kwargs
        )
        assert "last: Connection timed out" in line
        assert "Ctrl+C" not in line
        assert "amind@amin-desktop" not in line
        assert "attempt 4" in line
        assert "retry in 16s" in line

    def test_the_numbers_are_the_last_thing_standing(self):
        # Below every other sacrifice, "this pane is alive and will try again"
        # is the whole message.
        line = attach_client.status_text(
            target="amind@amin-desktop",
            attempt=4,
            remaining=16.0,
            last_error="Connection timed out",
            width=42,
        )
        assert "attempt 4" in line
        assert "retry in 16s" in line
        assert "last:" not in line

    def test_a_finished_countdown_reads_as_dialing(self):
        # The line stays on screen for the whole of the next dial (up to a 20s
        # ConnectTimeout), so it must not keep claiming "retry in 0s".
        line = attach_client.status_text(target="user@host", attempt=2, width=120)
        assert "dialing" in line
        assert "retry in" not in line

    def test_it_is_plain_text_with_no_escape_sequences_baked_in(self):
        # Coloring happens in the writer, over the already-clipped string, so
        # len() stays the rendered width.
        line = attach_client.status_text(
            target="user@host", attempt=1, remaining=2.0, width=120
        )
        assert "\x1b" not in line


class TestCondenseError:
    """ssh's diagnostics, squeezed into a status-line clause."""

    def test_it_keeps_only_the_part_of_an_ssh_error_that_varies(self):
        assert (
            attach_client.condense_error(
                "ssh: connect to host amin-desktop port 22: Connection timed out"
            )
            == "Connection timed out"
        )

    def test_a_non_ssh_prefixed_line_is_kept_whole(self):
        assert (
            attach_client.condense_error("client_loop: send disconnect: Broken pipe")
            == "client_loop: send disconnect: Broken pipe"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "Connection closed by\n1.2.3.4 port 22",
            "Connection closed by\t1.2.3.4 port 22",
            "  Connection closed by 1.2.3.4 port 22  ",
        ],
    )
    def test_it_flattens_anything_that_would_break_the_line_in_two(self, raw):
        out = attach_client.condense_error(raw)
        assert out == "Connection closed by 1.2.3.4 port 22"

    def test_it_is_ascii_so_len_is_the_rendered_width(self):
        # The clipping math counts characters; a multi-byte or wide glyph from
        # a chatty sshd would make the "one row" promise a lie.
        out = attach_client.condense_error("Connection closed by — hôte")
        assert out.isascii()

    def test_a_pathological_line_is_elided_not_wrapped(self):
        out = attach_client.condense_error("x" * 500)
        assert len(out) <= attach_client.LAST_ERROR_MAX


class TestStatusLineWriter:
    """The terminal half: WHERE the status line is allowed to draw.

    When ssh dies mid-session the terminal is still showing the remote TUI's
    frozen last frame, with the cursor parked in the user's half-typed prompt.
    The first version of this repainted with a bare ``\\r\\x1b[2K`` right there
    and erased the sentence the user was about to send -- the reported bug. The
    contract below is what replaced it, and it is stated in escape sequences
    because that is what the guarantee is made of; the same guarantee stated as
    "the text is still on screen" lives in the real-pty tier
    (``tests/e2e/test_pty_attach_status.py``).
    """

    def test_every_repaint_erases_only_after_jumping_to_the_bottom_row(
        self, capsys, monkeypatch
    ):
        # THE REGRESSION GUARD. An erase that is not preceded by an absolute
        # move to the bottom row is an erase landing on whatever row the dead
        # TUI left the cursor on.
        monkeypatch.setattr(attach_client, "_term_rows", lambda: 24)
        line = attach_client.StatusLine(tty=True)
        line.show("first")
        line.show("second")
        out = capsys.readouterr().out
        home = attach_client.SAVE_CURSOR + "\x1b[24;1H" + attach_client.ERASE_LINE
        assert out.count(attach_client.ERASE_LINE) == 2
        assert out.count(home) == 2
        # Every repaint erases before it writes, so the row shows only the
        # newest text -- never "second" printed over the tail of "first".
        assert out.index("second") > out.index("first")
        assert out.rindex(home) < out.index("second")

    def test_the_old_erase_at_the_cursor_repaint_is_gone(self, capsys):
        # Spelled out rather than implied: this exact sequence is the bug.
        attach_client.StatusLine(tty=True).show("anything")
        assert "\r\x1b[2K" not in capsys.readouterr().out

    def test_not_one_newline_is_ever_emitted(self, capsys):
        # "Scroll a fresh row into existence" is the obvious way to own a line
        # and it is wrong here: the alternate screen has no scrollback, so a
        # scroll destroys the frame's top row and shoves everything else up.
        line = attach_client.StatusLine(tty=True)
        line.show("first")
        line.show("second")
        line.clear()
        assert "\n" not in capsys.readouterr().out

    def test_the_frames_own_cursor_is_put_back_after_every_write(self, capsys):
        line = attach_client.StatusLine(tty=True)
        line.show("x")
        out = capsys.readouterr().out
        assert out.startswith(attach_client.SAVE_CURSOR)
        assert out.endswith(attach_client.RESTORE_CURSOR)

    def test_the_bottom_row_is_re_read_so_a_resized_pane_is_followed(
        self, capsys, monkeypatch
    ):
        # A pane can be retiled mid-outage; a status line still addressed at the
        # old bottom row would start erasing a row inside the frozen frame.
        line = attach_client.StatusLine(tty=True)
        monkeypatch.setattr(attach_client, "_term_rows", lambda: 24)
        line.show("x")
        monkeypatch.setattr(attach_client, "_term_rows", lambda: 50)
        line.show("x")
        out = capsys.readouterr().out
        assert "\x1b[24;1H" in out
        assert "\x1b[50;1H" in out

    def test_clearing_erases_only_a_line_that_was_drawn(self, capsys, monkeypatch):
        monkeypatch.setattr(attach_client, "_term_rows", lambda: 24)
        line = attach_client.StatusLine(tty=True)
        line.clear()
        assert capsys.readouterr().out == ""
        line.show("x")
        capsys.readouterr()
        line.clear()
        assert capsys.readouterr().out == (
            attach_client.SAVE_CURSOR
            + "\x1b[24;1H"
            + attach_client.ERASE_LINE
            + attach_client.RESTORE_CURSOR
        )
        # ...and a second clear is a no-op, so it cannot blank whatever the
        # restored remote session printed next.
        line.clear()
        assert capsys.readouterr().out == ""

    def test_a_redirected_pane_gets_no_escape_sequences_at_all(self, capsys):
        # Cursor animation in a log file is unreadable garbage.
        line = attach_client.StatusLine(tty=False)
        line.show("anything")
        line.clear()
        assert capsys.readouterr().out == ""


class TestBottomRowProbe:
    """``_term_rows``: which row the status line is allowed to own."""

    def test_it_reports_the_terminals_real_height(self, monkeypatch):
        monkeypatch.setattr(
            attach_client.shutil,
            "get_terminal_size",
            lambda **_k: os.terminal_size((120, 40)),
        )
        assert attach_client._term_rows() == 40

    @pytest.mark.parametrize("boom", [OSError("no tty"), ValueError("nonsense")])
    def test_a_terminal_that_cannot_answer_falls_back_rather_than_crashing(
        self, monkeypatch, boom
    ):
        def raise_it(**_kwargs):
            raise boom

        monkeypatch.setattr(attach_client.shutil, "get_terminal_size", raise_it)
        assert attach_client._term_rows() == 24

    def test_a_nonsense_height_never_builds_a_row_zero_move(self, monkeypatch):
        # `\x1b[0;1H` is not a row on any terminal; a pane being resized can
        # briefly report a height of 0.
        monkeypatch.setattr(
            attach_client.shutil,
            "get_terminal_size",
            lambda **_k: os.terminal_size((80, 0)),
        )
        assert attach_client._term_rows() >= 1
        assert "[0;1H" not in attach_client.status_home()


class TestQuietReconnects:
    """The reported UX bug: a wi-fi outage scrolled three lines per attempt."""

    def test_a_tty_outage_costs_one_line_no_matter_how_long_it_lasts(
        self, monkeypatch, capsys
    ):
        rc, _calls, _sleeps = _drive(monkeypatch, [255] * 6 + [0], tty=True)
        out = capsys.readouterr().out
        # Not one newline is emitted while the pane heals itself, so everything
        # before the closing "detached" message is repaint traffic on a single
        # row -- 42 seconds of backoff across six attempts, one line.
        healing, _, rest = out.partition("\n")
        assert rc == 0
        assert healing.count(attach_client.ERASE_LINE) > 6
        assert "detached from" in rest

    def test_a_redirected_pane_falls_back_to_one_plain_line_per_attempt(
        self, monkeypatch, capsys
    ):
        rc, _calls, sleeps = _drive(monkeypatch, [255, 255, 0], tty=False)
        out = capsys.readouterr().out
        assert rc == 0
        assert attach_client.ERASE_LINE not in out
        assert out.count("reconnecting to user@host") == 2
        # One sleep per backoff, not one per second: a log does not animate.
        assert sleeps == [2.0, 4.0]

    def test_the_countdown_ticks_down_in_place_during_the_backoff(
        self, monkeypatch, capsys
    ):
        rc, _calls, sleeps = _drive(monkeypatch, [255, 0], tty=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert sleeps == [1.0, 1.0]  # 2s backoff, 1s granularity
        assert "retry in 2s" in out
        assert "retry in 1s" in out
        assert "dialing" in out

    def test_ssh_s_own_last_word_becomes_the_status_line_s_reason(
        self, monkeypatch, capsys
    ):
        # The noise the user reported comes from the ssh CHILD. Captured, it
        # stops scrolling and reappears as one clause on the one line.
        rc, _calls, _sleeps = _drive(
            monkeypatch,
            [
                _dial(255, "ssh: connect to host box port 22: Connection timed out"),
                _dial(0),
            ],
            tty=True,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "last: Connection timed out" in out

    def test_a_healed_outage_leaves_exactly_one_permanent_line(
        self, monkeypatch, capsys
    ):
        # The record is written at the drop that ENDED the restored session,
        # never the moment it came back: at that moment ssh owns the console
        # and the remote psmux is repainting an alternate screen, so anything
        # printed lands inside the user's agent pane as unrepairable garbage.
        rc, _calls, _sleeps = _drive(
            monkeypatch,
            [255, 255, 255, 0],
            durations=[0.1, 0.1, 3600.0, 0.0],
            tty=True,
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert out.count("reconnected to user@host") == 1
        assert "after 2 attempt(s); stayed up 1h00m" in out

    def test_a_first_connection_that_never_dropped_is_not_announced_as_healed(
        self, monkeypatch, capsys
    ):
        _rc, _calls, _sleeps = _drive(monkeypatch, [0], durations=[3600.0], tty=True)
        assert "reconnected to" not in capsys.readouterr().out

    def test_a_pane_that_gives_up_hands_back_what_ssh_actually_said(
        self, monkeypatch, capsys
    ):
        # Swallowing ssh's stderr is only defensible because a pane that STOPS
        # trying flushes it: otherwise the last thing a dead pane shows is a
        # message we wrote, and the real diagnostic is gone.
        rc, _calls, _sleeps = _drive(
            monkeypatch,
            [_dial(0, "boom", ("psmux: no server running", "boom"))]
            * attach_client.SESSION_MISSING_MAX,
            probes=[attach_client.SESSION_GONE] * attach_client.SESSION_MISSING_MAX,
            tty=True,
        )
        out = capsys.readouterr().out
        assert rc == 1
        assert "ssh said:" in out
        assert "psmux: no server running" in out

    def test_no_reconnect_keeps_ssh_s_stderr_on_ssh_s_stderr(self, monkeypatch):
        # That flag's promise is the historical bare-ssh pane, down to which fd
        # ssh's errors land on -- so it must never pipe them.
        captured: list[bool] = []

        def spy(_argv, *, capture=False):
            captured.append(capture)
            return _dial(255)

        monkeypatch.setattr(attach_client.shutil, "which", lambda _n: "/usr/bin/ssh")
        monkeypatch.setattr(attach_client, "_stdout_is_tty", lambda: True)
        monkeypatch.setattr(attach_client, "_run_ssh", spy)
        attach_client.supervise(
            "user@host", "psmux -L api attach", "api", reconnect=False
        )
        assert captured == [False]


class TestStderrCapture:
    """Why piping ssh's fd 2 is safe: prompts use the tty, and with ``-t`` the
    remote session's output arrives on STDOUT. Only client diagnostics land
    here, which is exactly what the status line reports."""

    def test_it_keeps_the_last_line_and_a_bounded_tail(self):
        pump = attach_client._StderrPump(
            io.BytesIO(b"first\nsecond\n\nthird\n"),
        )
        last, tail = pump.close()
        assert last == "third"
        assert tail == ("first", "second", "third")

    def test_the_tail_cannot_grow_without_bound(self):
        blob = b"".join(f"line {n}\n".encode() for n in range(200))
        _last, tail = attach_client._StderrPump(io.BytesIO(blob)).close()
        assert len(tail) == attach_client.STDERR_TAIL_LINES
        assert tail[-1] == "line 199"

    def test_undecodable_bytes_never_crash_the_pane(self):
        last, _tail = attach_client._StderrPump(io.BytesIO(b"\xff\xfe bad\n")).close()
        assert last.endswith("bad")

    def test_a_changed_host_key_is_never_swallowed(self, capsys):
        # The one exception to "quiet during an outage": a changed host key is
        # a security event, and a 255 loop may never reach a stop path that
        # would flush the tail. Safe to print because host-key verification
        # fails during the handshake -- there is no live session to corrupt.
        attach_client._StderrPump(
            io.BytesIO(b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"),
        ).close()
        assert "REMOTE HOST IDENTIFICATION HAS CHANGED" in capsys.readouterr().err

    def test_the_interactive_child_never_has_its_stdin_or_stdout_piped(
        self, monkeypatch
    ):
        # The module's whole contract: a waiter, never a middleman. Piping
        # stdout would cost the session its colors, mouse reporting and resize.
        seen: dict[str, object] = {}

        class _Proc:
            stderr = None

            def wait(self):
                return 0

        def fake_popen(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return _Proc()

        monkeypatch.setattr(attach_client.subprocess, "Popen", fake_popen)
        attach_client._run_ssh(["ssh", "host"], capture=True)
        assert "stdin" not in seen
        assert "stdout" not in seen
        assert seen["stderr"] == subprocess.PIPE

    def test_without_capture_even_stderr_stays_inherited(self, monkeypatch):
        seen: dict[str, object] = {}

        class _Proc:
            stderr = None

            def wait(self):
                return 3

        monkeypatch.setattr(
            attach_client.subprocess,
            "Popen",
            lambda argv, **kw: (seen.update(argv=argv, **kw), _Proc())[1],
        )
        assert attach_client._run_ssh(["ssh", "host"]).rc == 3
        assert seen["stderr"] is None


class TestArgumentParsing:
    def test_the_spawned_shape_round_trips(self):
        opts = attach_client.parse_args(
            [
                "--target",
                "me@box",
                "--session",
                "api",
                "--remote",
                "psmux -L api attach || magent sessions api",
            ]
        )
        assert opts.target == "me@box"
        assert opts.session == "api"
        assert opts.remote == "psmux -L api attach || magent sessions api"
        assert opts.reconnect is True

    def test_remote_defaults_to_the_session_attach_command(self):
        opts = attach_client.parse_args(["--target", "me@box", "--session", "api"])
        assert opts.remote == attach_client.remote_attach_command("api")

    def test_no_reconnect_turns_the_loop_off(self):
        opts = attach_client.parse_args(
            ["--target", "me@box", "--session", "api", "--no-reconnect"]
        )
        assert opts.reconnect is False

    @pytest.mark.parametrize("args", [[], ["--target", "me@box"], ["--session", "api"]])
    def test_a_half_specified_invocation_is_rejected(self, args):
        with pytest.raises(SystemExit):
            attach_client.parse_args(args)


class TestMain:
    def test_ctrl_c_stops_cleanly_without_a_traceback(self, monkeypatch, capsys):
        def boom(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(attach_client, "supervise", boom)
        rc = attach_client.main(["--target", "me@box", "--session", "api"])
        assert rc == 130
        assert "Stopped." in capsys.readouterr().out

    def test_it_forwards_the_parsed_options_to_the_loop(self, monkeypatch):
        seen: dict[str, object] = {}

        def spy(target, remote, session, *, reconnect):
            seen.update(
                target=target, remote=remote, session=session, reconnect=reconnect
            )
            return 0

        monkeypatch.setattr(attach_client, "supervise", spy)
        rc = attach_client.main(
            ["--target", "me@box", "--session", "api", "--no-reconnect"]
        )
        assert rc == 0
        assert seen == {
            "target": "me@box",
            "remote": attach_client.remote_attach_command("api"),
            "session": "api",
            "reconnect": False,
        }


class TestRemoteCommandContract:
    def test_it_still_prefers_a_direct_psmux_attach(self):
        # Booting the full magent CLI per window (python import + config load,
        # x40 windows) is what once made a big attach take minutes; the picker
        # is the fallback for a session id the host no longer has.
        assert (
            attach_client.remote_attach_command("api")
            == "psmux -L api attach || magent sessions api"
        )

    def test_the_command_is_recognised_by_the_corpse_markers(self):
        # The two halves of the same contract live in different modules; this
        # is the test that fails if either one is edited alone.
        from magent.cli import attach as attach_mod

        cmd = attach_client.remote_attach_command("api")
        assert any(m in cmd for m in attach_mod._attach_markers("api"))
