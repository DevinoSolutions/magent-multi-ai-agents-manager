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


def _drive(monkeypatch, codes, *, durations=None, probes=None, **kwargs):
    """Run ``supervise`` against a scripted sequence of ssh exit codes.

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

    def fake_run(argv):
        calls.append(list(argv))
        if not remaining:
            raise AssertionError("supervise asked for more connections than scripted")
        now[0] += clock.pop(0) if clock else 0.0
        return remaining.pop(0)

    def fake_probe(_target, _session):
        return answers.pop(0) if answers else attach_client.SESSION_ALIVE

    monkeypatch.setattr(attach_client.shutil, "which", lambda _n: "/usr/bin/ssh")
    monkeypatch.setattr(attach_client, "_run_ssh", fake_run)
    monkeypatch.setattr(attach_client, "_probe_session", fake_probe)
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
        assert "reconnecting in 2s" in out
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
        monkeypatch.setattr(attach_client, "_run_ssh", lambda _argv: 1)
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
            "call",
            lambda _argv: (_ for _ in ()).throw(FileNotFoundError()),
        )
        assert attach_client._run_ssh(["ssh"]) == attach_client.SSH_MISSING_RC


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
