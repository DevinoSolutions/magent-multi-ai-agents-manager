"""Session-picker load hardening: the parallel liveness sweep retries flapping
probes, direct-name attach resolves from config (no sweep dependency), and a
failed attach is surfaced + retried instead of being wiped by the redraw.
"""

from __future__ import annotations

from magent.cli import session_picker


class TestLiveSessions:
    def test_retries_misses_once(self, monkeypatch):
        # First probe flaps b and c to False; the retry recovers b.
        calls: list[list[str]] = []
        flaky = {"a": [True], "b": [False, True], "c": [False, False]}

        def fake_has_session(name, psmux=None):
            calls.append([name])
            return flaky[name].pop(0)

        monkeypatch.setattr(
            "magent.psmux.has_session",
            lambda n, psmux=None: fake_has_session(n, psmux),
        )
        live = session_picker._live_sessions("psmux", ["a", "b", "c"])
        assert live == ["a", "b"]

    def test_config_order_preserved(self, monkeypatch):
        monkeypatch.setattr("magent.psmux.has_session", lambda n, psmux=None: True)
        live = session_picker._live_sessions("psmux", ["z", "m", "a"])
        assert live == ["z", "m", "a"]

    def test_no_retry_when_all_alive(self, monkeypatch):
        counts: dict[str, int] = {}

        def fake(n, psmux=None):
            counts[n] = counts.get(n, 0) + 1
            return True

        monkeypatch.setattr("magent.psmux.has_session", fake)
        session_picker._live_sessions("psmux", ["a", "b"])
        assert counts == {"a": 1, "b": 1}


class TestConfigSessionCandidates:
    def test_filters_and_sanitizes(self):
        data = {
            "settings": {"defaultTool": "claude"},
            "projects": [
                {"path": "x/api proj"},
                {"path": "x/web", "tool": "vscode"},
                {"path": "x/off", "enabled": False},
                {"path": "x/y", "title": "My.App"},
            ],
        }
        assert session_picker._config_session_candidates(data) == [
            "api-proj",
            "My-App",
        ]


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
