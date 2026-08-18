"""Session-picker load hardening and first-paint cost: per-session cwds come
from config rather than a psmux probe per paint, direct-name attach resolves
from config (no sweep dependency), and a failed attach is surfaced + retried
instead of being wiped by the redraw.

The liveness sweep itself is no longer this module's -- it is
`psmux.live_sessions`, pinned by `test_psmux.py::TestLiveSessions`. The picker
owning the product's only retrying probe is exactly how `status`/`down` came to
disagree with it about which sessions exist.
"""

from __future__ import annotations

import time

from magent import psmux as psmux_mod
from magent.cli import session_picker


class TestPickerSweepIsTheSharedSeam:
    def test_the_picker_sweeps_through_psmux_live_sessions(
        self, monkeypatch, tmp_config
    ):
        # A pin on the seam, not on the probe: whatever the shared enumeration
        # answers is what the picker lists, so the picker can never again be
        # the one surface that sees a session `down` will skip.
        from pathlib import Path

        seen: list[tuple[list[str], str | None]] = []
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux_mod,
            "live_sessions",
            lambda names, psmux=None, **kw: (seen.append((list(names), psmux)), [])[1],
        )
        cfgpath = tmp_config({"projects": [{"path": "api"}, {"path": "web"}]})
        session_picker._run_sessions_picker(Path(cfgpath))
        assert seen == [(["api", "web"], "psmux")]


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
