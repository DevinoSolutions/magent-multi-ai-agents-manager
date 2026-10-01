"""What a ``has-session -t`` exit code says about its session.

``has-session`` has exactly two answers: 0 (there) and 1 (no such session). A
client that dies any other way -- STATUS_DLL_INIT_FAILED (0xC0000142), an
access violation, a signal, a launcher that cannot start it -- has said nothing
about the session it was asked about, and on the 2026-08-18 wedge the sockets
that stopped answering were FROZEN live agents. ``absent`` is the one answer
that may lead the bring-up to kill a socket and create it afresh, so it is
reserved for the documented rc 1.

The bring-up path itself (a REAL client, a crashed one, through
``launch_psmux_session`` and ``launch_verified``) is pinned in
``test_bringup_bounded_waits.py``; this file pins the rc table and every other
caller of the probe seam.
"""

from __future__ import annotations

import subprocess

import pytest

from magent import psmux
from magent.config import MagentConfig, ProjectConfig

# 0xC0000142 as the two ways a caller can meet it: the unsigned NTSTATUS
# Windows reports for a client that could not initialize its DLLs, and the
# signed 32-bit twin some runtimes hand back.
_DLL_INIT_FAILED = 0xC0000142
_DLL_INIT_FAILED_SIGNED = _DLL_INIT_FAILED - (1 << 32)


class _Client:
    """A finished ``has-session`` client with a scripted exit code.

    ``rc=None`` is a client that never answers: ``wait`` outruns its timeout.
    """

    def __init__(self, rc: int | None) -> None:
        self.rc = rc
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.rc is None:
            raise subprocess.TimeoutExpired("psmux", timeout or 0)
        return self.rc

    def kill(self) -> None:
        self.killed = True


def _clients(monkeypatch: pytest.MonkeyPatch, codes: dict[str, int | None]):
    """Every ``psmux -L <name> has-session ...`` spawn exits ``codes[name]``."""
    spawned: list[list[str]] = []

    def _popen(argv, **_kw):
        spawned.append(list(argv))
        return _Client(codes[argv[2]])

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return spawned


class TestTheAbsentAnswerIsExactlyRcOne:
    @pytest.mark.parametrize(
        ("rc", "state"),
        [
            (0, "live"),
            (1, "absent"),
            (None, "unknown"),  # never answered / could not be spawned
            (2, "unknown"),  # a usage error
            (70, "unknown"),  # a harness-style internal error
            (255, "unknown"),  # ssh-style "the transport failed"
            (-1, "unknown"),
            (-9, "unknown"),  # killed by SIGKILL (POSIX signal codes are negative)
            (_DLL_INIT_FAILED, "unknown"),
            (_DLL_INIT_FAILED_SIGNED, "unknown"),
            (0xC0000005, "unknown"),  # STATUS_ACCESS_VIOLATION, unsigned
            (0xC0000005 - (1 << 32), "unknown"),  # ...and signed
        ],
    )
    def test_the_rc_table(self, rc, state):
        assert psmux.session_state_from_rc(rc) == state

    def test_the_documented_absent_code_is_one(self):
        # tmux's own, measured on psmux 3.3.x. Moving this moves what the
        # bring-up is allowed to kill.
        assert psmux.HAS_SESSION_ABSENT_RC == 1

    def test_the_probe_reads_every_client_through_the_table(self, monkeypatch):
        codes = {
            "up": 0,
            "gone": 1,
            "hung": None,
            "crashed": 70,
            "dll": _DLL_INIT_FAILED,
            "dll-signed": _DLL_INIT_FAILED_SIGNED,
        }
        spawned = _clients(monkeypatch, codes)
        states = psmux.probe_sessions(list(codes), "psmux", timeout=0.5)
        assert states == {
            "up": "live",
            "gone": "absent",
            "hung": "unknown",
            "crashed": "unknown",
            "dll": "unknown",
            "dll-signed": "unknown",
        }
        # The probe is still `-t`-targeted: a bare has-session exits 0 for a
        # socket with no server at all.
        assert spawned[0] == ["psmux", "-L", "up", "has-session", "-t", "up"]

    def test_a_crashed_client_is_named_in_the_launch_log(self, monkeypatch):
        # An unexplained "unknown" would read as a wedge; the log says which
        # code, in the form a Windows crash is looked up by.
        _clients(monkeypatch, {"web": _DLL_INIT_FAILED})
        seen: list[str] = []

        class _Log:
            def warning(self, msg, *args):
                seen.append(msg % args)

        monkeypatch.setattr(psmux, "get_logger", lambda _name: _Log())
        psmux.probe_sessions(["web"], "psmux", timeout=0.5)
        assert len(seen) == 1
        assert "0xc0000142" in seen[0]
        assert "unknown, not absent" in seen[0]

    def test_a_plain_absent_or_live_answer_is_not_logged(self, monkeypatch):
        _clients(monkeypatch, {"a": 0, "b": 1, "c": None})
        seen: list[str] = []

        class _Log:
            def warning(self, msg, *args):
                seen.append(msg % args)

        monkeypatch.setattr(psmux, "get_logger", lambda _name: _Log())
        psmux.probe_sessions(["a", "b", "c"], "psmux", timeout=0.5)
        assert seen == []


class TestEveryOtherProbeFoldsACrashIntoNotLive:
    """The boolean seams answer "is it running", and fold what they cannot
    read into NO. That is the right fold for every surface that reads (status,
    the picker, ``sessions --json``, upload discovery) and for revive, which
    only ever types into a session it proved live. It is safe for the
    bring-up's creation verify only because the respawn it triggers goes back
    through ``probe_sessions`` -- pinned in test_bringup_bounded_waits.py.
    """

    @pytest.mark.parametrize(
        "rc", [70, _DLL_INIT_FAILED, _DLL_INIT_FAILED_SIGNED, 2, -9]
    )
    def test_has_session_reads_a_crash_as_not_live(self, monkeypatch, rc):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], rc),
        )
        assert psmux.has_session("web", psmux="psmux") is False

    @pytest.mark.parametrize("rc", [70, _DLL_INIT_FAILED, _DLL_INIT_FAILED_SIGNED])
    def test_live_sessions_never_reports_a_crash_live_and_retries_it_once(
        self, monkeypatch, rc
    ):
        spawned = _clients(monkeypatch, {"api": 0, "web": rc})
        assert psmux.live_sessions(["api", "web"], psmux="psmux") == ["api"]
        # api answered once. web was asked, then asked again (the flap retry),
        # and a crash on the retry is still not live.
        asked = [argv[2] for argv in spawned]
        assert asked.count("api") == 1
        assert asked.count("web") == 2

    def test_a_flapping_client_that_answers_on_the_retry_is_live(self, monkeypatch):
        # The other direction of "never treated as live WITHOUT a retry": the
        # first probe crashes, the retry answers 0, and the session is live.
        answers = iter([_DLL_INIT_FAILED, 0])
        spawned: list[list[str]] = []

        def _popen(argv, **_kw):
            spawned.append(list(argv))
            return _Client(next(answers))

        monkeypatch.setattr(subprocess, "Popen", _popen)
        assert psmux.live_sessions(["web"], psmux="psmux") == ["web"]
        assert len(spawned) == 2

    def test_stop_sessions_kills_every_name_and_never_claims_a_crash_stopped(
        self, monkeypatch
    ):
        # `down` kills every configured name whatever the probe said, so a
        # crashed probe can never be the reason a session survives a shutdown;
        # and the report only claims what a probe PROVED was live and is gone.
        killed: list[list[str]] = []
        monkeypatch.setattr(psmux, "_STOP_SETTLE_S", 0.0)
        monkeypatch.setattr(
            psmux, "_kill_batch", lambda names, _bin: killed.append(list(names))
        )
        _clients(monkeypatch, {"api": _DLL_INIT_FAILED, "web": 1})
        stopped, still = psmux.stop_sessions(["api", "web"], psmux="psmux")
        assert killed == [["api", "web"]]
        assert stopped == []  # nothing was PROVEN live before the kill
        assert still == []

    def test_revive_never_types_into_a_session_whose_probe_crashed(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess([], _DLL_INIT_FAILED),
        )
        monkeypatch.setattr(
            psmux, "send_keys", lambda name, *_k, **_kw: sent.append(name) or True
        )
        # idle_sessions is asked only about LIVE sessions; a crashed probe
        # must not reach it (it would be asked to judge a pane it cannot read).
        asked: list[list[str]] = []
        monkeypatch.setattr(
            psmux,
            "idle_sessions",
            lambda names, **_k: asked.append(list(names)) or set(names),
        )
        cfg = MagentConfig(projects=[ProjectConfig(path="/a/api", tool="claude")])
        assert psmux.revive_sessions(cfg) == []
        assert sent == []
        assert asked == [[]]
