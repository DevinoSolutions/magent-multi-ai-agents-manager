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

import pytest

from magent import attach_client


class TestVerdict:
    """What one ssh exit code means. Three outcomes; getting the middle one
    wrong either abandons a healable pane or hot-loops on someone's sshd."""

    def test_clean_exit_is_a_deliberate_detach(self):
        # `psmux detach`, or the picker quitting: the user asked to leave.
        assert attach_client.verdict(0) == attach_client.DETACHED

    def test_255_is_a_transport_failure_worth_retrying(self):
        # `client_loop: send disconnect`, connection refused, host unreachable,
        # DNS, auth -- OpenSSH reserves 255 for all of its own failures.
        assert attach_client.verdict(255) == attach_client.RECONNECT

    @pytest.mark.parametrize("rc", [1, 2, 126, 127, 130, 254])
    def test_any_other_code_came_from_the_remote_command(self, rc):
        # The CONNECTION worked; the remote side failed. Reconnecting would
        # re-run a command that just failed for a reason a retry cannot fix.
        assert attach_client.verdict(rc) == attach_client.REMOTE_FAILED

    @pytest.mark.parametrize("rc", [-15, -9, 3840, 3221225786])
    def test_a_killed_ssh_child_stops_rather_than_redialling(self, rc):
        # POSIX subprocess reports -signum; Windows reports the raw code
        # (3221225786 == STATUS_CONTROL_C_EXIT, 3840 seen from a harness kill).
        # Something deliberately killed the client -- a supervisor that
        # answered a kill by dialling again would be unstoppable.
        assert attach_client.verdict(rc) == attach_client.REMOTE_FAILED


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


def _drive(monkeypatch, codes, *, durations=None, **kwargs):
    """Run ``supervise`` against a scripted sequence of ssh exit codes.

    Returns ``(rc, calls, sleeps)``: the supervisor's own exit code, the argv
    of every ssh it ran, and every backoff it waited out. A sequence that runs
    dry is a test bug (the loop only ends on a stopping verdict), so it raises
    rather than hanging.
    """
    remaining = list(codes)
    clock = list(durations or [])
    calls: list[list[str]] = []
    sleeps: list[float] = []
    now = [0.0]

    def fake_run(argv):
        calls.append(list(argv))
        if not remaining:
            raise AssertionError("supervise asked for more connections than scripted")
        now[0] += clock.pop(0) if clock else 0.0
        return remaining.pop(0)

    monkeypatch.setattr(attach_client.shutil, "which", lambda _n: "/usr/bin/ssh")
    monkeypatch.setattr(attach_client, "_run_ssh", fake_run)
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

    def test_a_failing_remote_command_stops_instead_of_hot_looping(
        self, monkeypatch, capsys
    ):
        # The host answered -- the connection is fine. Retrying would hammer a
        # perfectly healthy sshd forever over a session that no longer exists.
        rc, calls, sleeps = _drive(monkeypatch, [1])
        assert (rc, len(calls), sleeps) == (1, 1, [])
        out = capsys.readouterr().out
        assert "could not be attached" in out
        assert "magent attach" in out

    def test_no_reconnect_reports_the_drop_and_exits(self, monkeypatch, capsys):
        rc, calls, sleeps = _drive(monkeypatch, [255], reconnect=False)
        assert (rc, len(calls), sleeps) == (255, 1, [])
        assert "--no-reconnect" in capsys.readouterr().out

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
