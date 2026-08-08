"""Session-picker load hardening and first-paint cost: the liveness sweep is a
process fan-out that retries flapping probes, per-session cwds come from config
rather than a psmux probe per paint, direct-name attach resolves from config (no
sweep dependency), and a failed attach is surfaced + retried instead of being
wiped by the redraw.
"""

from __future__ import annotations

import time

from magent.cli import session_picker


class _FakeProc:
    """Stand-in for the Popen handle `_live_sessions` fans out, logging when it
    is waited on so the spawn-then-wait ordering can be pinned."""

    def __init__(self, name: str, returncode: int, events: list[str]) -> None:
        self._name = name
        self._returncode = returncode
        self._events = events

    def wait(self) -> int:
        self._events.append(f"wait:{self._name}")
        return self._returncode


def _fan_out(monkeypatch, results: dict[str, list[bool]]) -> list[str]:
    """Patch Popen so each `has-session` probe pops the next result for its
    session. Returns the spawn/wait event log."""
    events: list[str] = []

    def _fake_popen(cmd, **kwargs):
        name = cmd[2]
        events.append(f"spawn:{name}")
        return _FakeProc(name, 0 if results[name].pop(0) else 1, events)

    monkeypatch.setattr(session_picker.subprocess, "Popen", _fake_popen)
    return events


class TestLiveSessions:
    def test_retries_misses_once(self, monkeypatch):
        # First probe flaps b and c to False; the retry recovers b.
        _fan_out(monkeypatch, {"a": [True], "b": [False, True], "c": [False, False]})
        assert session_picker._live_sessions("psmux", ["a", "b", "c"]) == ["a", "b"]

    def test_config_order_preserved(self, monkeypatch):
        _fan_out(monkeypatch, {"z": [True], "m": [True], "a": [True]})
        assert session_picker._live_sessions("psmux", ["z", "m", "a"]) == [
            "z",
            "m",
            "a",
        ]

    def test_no_retry_when_all_alive(self, monkeypatch):
        events = _fan_out(monkeypatch, {"a": [True], "b": [True]})
        session_picker._live_sessions("psmux", ["a", "b"])
        assert [e for e in events if e.startswith("spawn:")] == ["spawn:a", "spawn:b"]

    def test_every_probe_is_spawned_before_any_is_waited_on(self, monkeypatch):
        # The point of the fan-out: n concurrent psmux round-trips, not n
        # sequential ones (nor ceil(n/16) thread-pool batches).
        events = _fan_out(monkeypatch, {"a": [True], "b": [True], "c": [True]})
        session_picker._live_sessions("psmux", ["a", "b", "c"])
        assert events == [
            "spawn:a",
            "spawn:b",
            "spawn:c",
            "wait:a",
            "wait:b",
            "wait:c",
        ]

    def test_probe_argv_is_a_per_session_has_session(self, monkeypatch):
        argvs: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            argvs.append(cmd)
            return _FakeProc(cmd[2], 0, [])

        monkeypatch.setattr(session_picker.subprocess, "Popen", _fake_popen)
        session_picker._live_sessions("psmux", ["a"])
        # `-t a` is load-bearing, not decoration: a BARE has-session exits 0
        # against a socket with no server at all (psmux 3.3.6 answers from its
        # internal __warm__ spare), so this sweep listed every configured
        # session as live and the picker offered dead ones for attaching.
        assert argvs == [["psmux", "-L", "a", "has-session", "-t", "a"]]

    def test_probes_run_with_the_nesting_markers_stripped(self, monkeypatch):
        # The picker is normally driven from inside a psmux window, whose env
        # carries PSMUX_SESSION/TMUX; a psmux child that sees them can refuse
        # to act on a sibling session.
        envs: list[object] = []

        def _fake_popen(cmd, **kwargs):
            envs.append(kwargs.get("env"))
            return _FakeProc(cmd[2], 0, [])

        monkeypatch.setenv("PSMUX_SESSION", "api")
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        monkeypatch.setenv("TMUX_TMPDIR", "/tmp/private-sockets")
        monkeypatch.setattr(session_picker.subprocess, "Popen", _fake_popen)
        session_picker._live_sessions("psmux", ["a"])
        assert envs and all(isinstance(e, dict) for e in envs)
        assert all("PSMUX_SESSION" not in e and "TMUX" not in e for e in envs)
        # ...but the socket dir survives, or the probe looks for the server in
        # the wrong place and reports every session dead.
        assert all(e["TMUX_TMPDIR"] == "/tmp/private-sockets" for e in envs)


class TestSessionCwds:
    def test_config_paths_are_used_without_probing_psmux(self, monkeypatch):
        # The whole perf fix: at 40 sessions the pane_cwd sweep was ~3.4s of a
        # ~4.8s first paint, and only restated what config already knew.
        probed: list[str] = []

        def _fake_pane_cwd(name, psmux=None):
            probed.append(name)
            return "/probed"

        monkeypatch.setattr("magent.psmux.pane_cwd", _fake_pane_cwd)
        cwds = session_picker._session_cwds(
            "psmux", ["a", "b"], {"a": "/proj/a", "b": "/proj/b"}
        )
        assert cwds == {"a": "/proj/a", "b": "/proj/b"}
        assert probed == []

    def test_only_an_unresolved_session_falls_back_to_a_probe(self, monkeypatch):
        probed: list[str] = []

        def _fake_pane_cwd(name, psmux=None):
            probed.append(name)
            return "/live/b"

        monkeypatch.setattr("magent.psmux.pane_cwd", _fake_pane_cwd)
        cwds = session_picker._session_cwds(
            "psmux", ["a", "b"], {"a": "/proj/a", "b": ""}
        )
        assert cwds == {"a": "/proj/a", "b": "/live/b"}
        assert probed == ["b"]

    def test_session_missing_from_the_map_falls_back_too(self, monkeypatch):
        monkeypatch.setattr("magent.psmux.pane_cwd", lambda n, psmux=None: "/live")
        assert session_picker._session_cwds("psmux", ["ghost"], {}) == {
            "ghost": "/live"
        }


class TestSessionStatuses:
    def test_state_is_looked_up_by_the_config_resolved_path(self, monkeypatch):
        seen: list[str] = []

        def _fake_state_for(cwd, max_age=None):
            seen.append(cwd)
            return {"state": "done", "ts": time.time()}

        monkeypatch.setattr("magent.agent_state.state_for", _fake_state_for)
        statuses = session_picker._session_statuses({"api": "C:/proj/api"})
        assert seen == ["C:/proj/api"]
        assert "done" in statuses["api"]

    def test_empty_cwd_is_never_looked_up(self, monkeypatch):
        def _boom(cwd, max_age=None):
            raise AssertionError(f"state_for called with {cwd!r}")

        monkeypatch.setattr("magent.agent_state.state_for", _boom)
        assert session_picker._session_statuses({"api": ""}) == {"api": ""}


class TestAttachSession:
    def _run(self, monkeypatch, rcs):
        seq = iter(rcs)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            session_picker.subprocess,
            "call",
            lambda cmd: (calls.append(cmd), next(seq))[1],
        )
        monkeypatch.setattr(session_picker.time, "sleep", lambda s: None)
        resets: list[bool] = []
        session_picker._attach_session("psmux", "sess", lambda: resets.append(True))
        return calls, resets

    def test_success_attaches_once(self, monkeypatch, capsys):
        calls, resets = self._run(monkeypatch, [0])
        assert len(calls) == 1 and calls[0] == ["psmux", "-L", "sess", "attach"]
        assert resets == [True]
        assert "failed" not in capsys.readouterr().out

    def test_failure_is_surfaced_and_retried(self, monkeypatch, capsys):
        calls, resets = self._run(monkeypatch, [1, 0])
        assert len(calls) == 2
        assert len(resets) == 2
        assert "retrying" in capsys.readouterr().out

    def test_double_failure_reports_overload(self, monkeypatch, capsys):
        calls, _ = self._run(monkeypatch, [1, 1])
        assert len(calls) == 2
        out = capsys.readouterr().out
        assert "failed twice" in out and "overloaded" in out
