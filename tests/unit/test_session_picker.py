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

import sys
import time
from pathlib import Path

import pytest

from magent import agent_state, attention
from magent import psmux as psmux_mod
from magent.cli import picker, session_picker


class TestPickerSweepIsTheSharedSeam:
    def test_the_picker_sweeps_through_psmux_live_sessions(
        self, monkeypatch, tmp_config
    ):
        # A pin on the seam, not on the probe: whatever the shared enumeration
        # answers is what the picker lists, so the picker can never again be
        # the one surface that sees a session `down` will skip.
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


class TestPickerStalenessComesFromConfig:
    """`settings.attention.stalenessWorkingS` / `stalenessNeedsInputS` decide
    what the picker calls stale -- not `attention.STALENESS_S`.

    The picker used to import the module default outright, so a user who widened
    the window got it honored by the attention daemon, `watch` and `status`'s
    agents array, and silently ignored by `magent sessions` (and by `status`'s
    psmux-session table, which reads these same helpers). The window map now
    arrives as a plain argument, built once by
    `attention_cmd.staleness_from_config`.
    """

    def _working_record(self, monkeypatch, age_s: float) -> None:
        monkeypatch.setattr(
            "magent.agent_state.state_for",
            lambda cwd, max_age=None: {
                "state": agent_state.WORKING,
                "ts": time.time() - age_s,
            },
        )

    def test_a_configured_window_ages_a_working_record(self, monkeypatch):
        # 120s old: far younger than the 1800s module default, well past a 10s
        # configured window -- so which map is in force decides the answer.
        self._working_record(monkeypatch, 120.0)

        states = session_picker._session_states(
            {"api": "/proj/api"}, {agent_state.WORKING: 10.0}
        )

        assert states["api"][0] is None

    def test_no_staleness_argument_falls_back_to_the_module_default(self, monkeypatch):
        # Named behaviour, not an accident: `_session_states` stays callable
        # without a config, and then the shipped defaults apply -- never a crash
        # and never a blank state column.
        default = attention.STALENESS_S[agent_state.WORKING]

        self._working_record(monkeypatch, default / 2)
        assert (
            session_picker._session_states({"api": "/proj/api"})["api"][0]
            == agent_state.WORKING
        )

        self._working_record(monkeypatch, default + 60)
        assert session_picker._session_states({"api": "/proj/api"})["api"][0] is None

    def test_the_picker_paints_the_configured_window_not_the_default(
        self, monkeypatch, tmp_config
    ):
        # The pin that fails on the shipped code: a `staleness` parameter nobody
        # threads through is still the bug, so this drives the real picker loop
        # and reads the LABEL it drew.
        self._working_record(monkeypatch, 120.0)
        rows = self._paint(
            monkeypatch,
            tmp_config(
                {
                    "projects": [{"path": "api"}],
                    "settings": {"attention": {"stalenessWorkingS": 10}},
                }
            ),
        )

        assert [r.label for r in rows] == ["api", "Back"]
        assert rows[0].extra == ""

    def test_the_default_config_still_paints_a_live_working_session(
        self, monkeypatch, tmp_config
    ):
        # Control for the above: the same 120s-old record under an untouched
        # config keeps its "still going..." label, so the blank column there is
        # genuinely config-driven aging and not a broken lookup.
        self._working_record(monkeypatch, 120.0)
        rows = self._paint(monkeypatch, tmp_config({"projects": [{"path": "api"}]}))

        assert "still going" in rows[0].extra

    def _paint(self, monkeypatch, cfgpath: str) -> list[picker.PickerItem]:
        """Run one picker iteration and return the rows it painted."""
        painted: list[list[picker.PickerItem]] = []
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux_mod, "live_sessions", lambda names, psmux=None, **kw: ["api"]
        )
        monkeypatch.setattr(
            session_picker, "_session_cwds", lambda *a: {"api": "/proj/api"}
        )
        monkeypatch.setattr(session_picker, "_consume_focus_target", lambda: None)
        monkeypatch.setattr(session_picker, "_running_upload_port", lambda: None)
        monkeypatch.setattr(
            session_picker,
            "_read_choice",
            lambda rows, url: (painted.append(rows), "q")[1],
        )

        session_picker._run_sessions_picker(Path(cfgpath))

        assert len(painted) == 1
        return painted[0]


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


@pytest.fixture
def fake_fleet(monkeypatch, tmp_config):
    """Drive `_run_sessions_picker` over a fake fleet with scripted typed lines.

    Characterization: stdin is NOT a terminal here, which is exactly the path a
    pipe / `CliRunner` / a script takes. The raw-key type-to-filter picker is
    gated on a real tty, so everything pinned through this fixture must stay
    byte-for-byte identical forever.
    """

    def _run(names, *answers, record_prompt=None):
        attached: list[str] = []
        seq = iter(answers)
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux_mod, "live_sessions", lambda live, psmux=None, **kw: list(names)
        )
        monkeypatch.setattr(
            session_picker,
            "_session_statuses",
            lambda cwds, staleness=None: dict.fromkeys(cwds, ""),
        )
        monkeypatch.setattr(session_picker, "_running_upload_port", lambda: None)
        monkeypatch.setattr(session_picker, "_consume_focus_target", lambda: None)
        monkeypatch.setattr(
            session_picker, "_attach_session", lambda b, t, r: attached.append(t)
        )
        monkeypatch.setattr(session_picker.click, "clear", lambda: None)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

        def _prompt(text, **kwargs):
            if record_prompt is not None:
                record_prompt.append(kwargs.get("default"))
            return next(seq)

        monkeypatch.setattr(session_picker.click, "prompt", _prompt)
        cfg = tmp_config({"projects": [{"path": n} for n in names]})
        session_picker._run_sessions_picker(Path(cfg))
        return attached

    return _run


class TestPickerLineInputIsUnchanged:
    def test_a_digit_attaches_to_that_row(self, fake_fleet):
        assert fake_fleet(["api", "web", "docs"], "2", "q") == ["web"]

    def test_q_returns_without_attaching(self, fake_fleet):
        assert fake_fleet(["api", "web"], "q") == []

    def test_a_substring_attaches_to_the_first_match(self, fake_fleet):
        assert fake_fleet(["api", "webapp", "web-docs"], "web", "q") == ["webapp"]

    def test_an_unmatchable_choice_reports_and_reprompts(self, fake_fleet, capsys):
        assert fake_fleet(["api"], "zzz", "q") == []
        assert "Invalid choice" in capsys.readouterr().out

    def test_out_of_range_digit_is_invalid(self, fake_fleet, capsys):
        assert fake_fleet(["api"], "9", "q") == []
        assert "Invalid choice" in capsys.readouterr().out

    def test_the_prompt_declares_1_as_its_default(self, fake_fleet):
        defaults: list[object] = []
        fake_fleet(["api"], "q", record_prompt=defaults)
        assert defaults == ["1"]
