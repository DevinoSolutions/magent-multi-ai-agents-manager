"""Resuming a session the idle reaper parked: ``psmux.revive_sessions``'s
``resume_parked`` gate.

A parked pane is an idle shell whose ``agent_state`` record says ``parked`` and
carries the stopped conversation's id. A bulk revive (``up``, attach's
``up --json --revive``) leaves it parked; only status's ``r<n>`` passes
``resume_parked=True``, and then the pane gets the EXACT conversation back by
id -- never ``--continue``, which could open another agent's. Driven through
the real ``revive_sessions``; only the pane probes, the process snapshot and
``send_keys`` are substituted, and the record store is conftest's tmp dir."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import magent
from magent import agent_state, launch, psmux
from magent.config import MagentConfig, ProjectConfig, Settings
from tests.unit._fake_panes import fake_panes, pane_tree

_SID = "11111111-2222-3333-4444-555555555555"
_PREFIX = "cmd /c " if sys.platform == "win32" else ""


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    """No test here may start a process. Every psmux call a revive makes is
    answered by a fake, and a missed one would reach the real psmux -- which,
    on a dev box, serves a live fleet. So ANY spawn (a bare ``psmux``, a
    PATH-resolved one, anything else) fails the test before the OS sees it.
    ``pytest.fail`` raises a BaseException, which no ``except Exception`` in
    the code under test can swallow."""

    def _refuse(args, *_a, **_kw):
        pytest.fail(f"test_reap_resume spawned a process: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", _refuse)


class _Fleet:
    """Two sessions, ``api`` and ``web``, in real tmp dirs (a project whose
    path is not a directory resolves to nothing and has no record to read);
    ``idle`` names the panes that fell back to a bare shell (the rest run
    claude.exe). ``sent`` records every send_keys as ``(session, keys,
    target)``; ``cmd`` is each session's configured command, what an ordinary
    revive types."""

    def __init__(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        idle=("api", "web"),
        tool="claude",
        sent_ok=True,
    ) -> None:
        self.sent: list[tuple[str, tuple[str, ...], str | None]] = []
        # An absolute path to nothing: a call the fakes miss cannot resolve
        # the real psmux off PATH (and ``_no_spawn`` refuses it first).
        missing = str(tmp_path / "no-such-psmux.exe")
        monkeypatch.setattr(psmux, "find_psmux", lambda: missing)
        monkeypatch.setattr(psmux, "has_session", lambda name, psmux=None: True)
        pids = {"api": 100, "web": 200}
        fake_panes(
            monkeypatch,
            foreground={n: "pwsh" if n in idle else "claude" for n in pids},
            pids=pids,
            snapshot=[
                e
                for n, pid in pids.items()
                for e in pane_tree(pid, *(() if n in idle else ("claude.exe",)))
            ],
        )

        def _send(name, *keys, target=None, psmux=None):
            self.sent.append((name, keys, target))
            return sent_ok

        monkeypatch.setattr(psmux, "send_keys", _send)
        self.cfg = MagentConfig(
            projects=[
                ProjectConfig(path=str(self._dir(tmp_path, "api")), tool=tool),
                ProjectConfig(path=str(self._dir(tmp_path, "web")), tool=tool),
            ],
            base_dir=None,
            settings=Settings(),
        )
        rows = psmux.eligible_projects(self.cfg)
        self.cwd = {str(p["session"]): str(p["resolved"]) for p in rows}
        self.cmd = {str(p["session"]): str(p["cmd"]) for p in rows}
        assert all(self.cwd.values()), self.cwd

    @staticmethod
    def _dir(tmp_path: Path, name: str) -> Path:
        d = tmp_path / name
        d.mkdir()
        return d

    def park(self, name: str, session_id: str | None = _SID) -> None:
        agent_state.write_state(self.cwd[name], agent_state.PARKED, session_id)

    def record(self, name: str) -> dict[str, object] | None:
        return agent_state.state_for(self.cwd[name])

    def typed(self, name: str) -> list[tuple[str, ...]]:
        return [keys for sess, keys, _ in self.sent if sess == name]


class TestAParkedSessionResumesOnlyWhenAsked:
    def test_status_resumes_it_by_id_and_clears_the_record(self, monkeypatch, tmp_path):
        fleet = _Fleet(monkeypatch, tmp_path)
        fleet.park("api")
        assert psmux.revive_sessions(fleet.cfg, only=["api"], resume_parked=True) == [
            "api"
        ]
        assert fleet.sent == [
            ("api", (f"{_PREFIX}claude --resume {_SID}", "Enter"), "api")
        ]
        assert fleet.record("api") is None

    def test_a_codex_session_resumes_with_its_own_verb(self, monkeypatch, tmp_path):
        fleet = _Fleet(monkeypatch, tmp_path, tool="codex")
        fleet.park("api")
        assert psmux.revive_sessions(fleet.cfg, only=["api"], resume_parked=True) == [
            "api"
        ]
        assert fleet.typed("api") == [(f"{_PREFIX}codex resume {_SID}", "Enter")]

    def test_a_bulk_revive_leaves_it_parked_and_revives_the_rest(
        self, monkeypatch, tmp_path
    ):
        fleet = _Fleet(monkeypatch, tmp_path)
        fleet.park("api")
        why: dict[str, str] = {}
        assert psmux.revive_sessions(fleet.cfg, vetoed=why) == ["web"]
        assert why == {"api": "the idle reaper parked it"}
        assert fleet.typed("api") == []
        assert fleet.typed("web") == [(f"{_PREFIX}{fleet.cmd['web']}", "Enter")]
        rec = fleet.record("api")
        assert rec is not None
        assert rec["state"] == agent_state.PARKED

    @pytest.mark.parametrize(
        "session_id",
        [None, "", "x & calc", "--dangerously-skip-permissions", "a b"],
        ids=["none", "empty", "shell-meta", "a-flag", "space"],
    )
    def test_no_usable_id_is_never_resumed_and_says_why(
        self, monkeypatch, tmp_path, caplog, session_id
    ):
        fleet = _Fleet(monkeypatch, tmp_path)
        fleet.park("api", session_id)
        why: dict[str, str] = {}
        with caplog.at_level("WARNING", logger="magent.launch"):
            assert (
                psmux.revive_sessions(
                    fleet.cfg, only=["api"], resume_parked=True, vetoed=why
                )
                == []
            )
        assert fleet.sent == []  # never falls through to --continue either
        # Status prints this for r<n>: not "its agent is still running".
        assert why == {"api": "it is parked without a resumable session id"}
        rec = fleet.record("api")
        assert rec is not None
        assert rec["state"] == agent_state.PARKED
        assert [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.launch" and r.levelname == "WARNING"
        ] == ["revive: api is parked without a resumable session id; leaving it"]

    def test_a_pane_with_a_live_agent_is_not_typed_into_even_when_parked(
        self, monkeypatch, tmp_path
    ):
        # The record says parked but an agent runs there now (started by hand):
        # the idle verdict comes first, so nothing is typed and nothing cleared.
        fleet = _Fleet(monkeypatch, tmp_path, idle=())
        fleet.park("api")
        why: dict[str, str] = {}
        assert psmux.revive_sessions(fleet.cfg, resume_parked=True, vetoed=why) == []
        assert fleet.sent == []
        assert why == dict.fromkeys(
            ("api", "web"), "its agent is still running, or its pane could not be read"
        )
        assert fleet.record("api") is not None

    def test_a_failed_send_keeps_the_record(self, monkeypatch, tmp_path):
        fleet = _Fleet(monkeypatch, tmp_path, sent_ok=False)
        fleet.park("api")
        why: dict[str, str] = {}
        assert (
            psmux.revive_sessions(
                fleet.cfg, only=["api"], resume_parked=True, vetoed=why
            )
            == []
        )
        assert why == {"api": "the resume could not be sent (see launch.log)"}
        rec = fleet.record("api")
        assert rec is not None
        assert rec["state"] == agent_state.PARKED

    @pytest.mark.parametrize("state", [agent_state.DONE, agent_state.IDLE, None])
    def test_any_other_record_gets_the_ordinary_revive(
        self, monkeypatch, tmp_path, state
    ):
        fleet = _Fleet(monkeypatch, tmp_path)
        if state is not None:
            agent_state.write_state(fleet.cwd["api"], state, _SID)
        assert psmux.revive_sessions(fleet.cfg, only=["api"], resume_parked=True) == [
            "api"
        ]
        assert fleet.typed("api") == [(f"{_PREFIX}{fleet.cmd['api']}", "Enter")]
        assert "--resume" not in fleet.cmd["api"]
        if state is not None:  # the ordinary revive never touches the store
            rec = fleet.record("api")
            assert rec is not None
            assert rec["state"] == state

    def test_only_the_parked_pane_is_cleared(self, monkeypatch, tmp_path):
        fleet = _Fleet(monkeypatch, tmp_path)
        fleet.park("api")
        fleet.park("web", "22222222-3333-4444-5555-666666666666")
        assert psmux.revive_sessions(fleet.cfg, only=["api"], resume_parked=True) == [
            "api"
        ]
        assert fleet.record("api") is None
        assert fleet.record("web") is not None


class TestWhoAsksForIt:
    def test_launch_passes_the_gate_through_and_defaults_it_off(self, monkeypatch):
        calls: list[tuple[object, object, bool]] = []

        def _revive(config, only=None, group=None, *, resume_parked=False):
            calls.append((only, group, resume_parked))
            return []

        monkeypatch.setattr(psmux, "revive_sessions", _revive)
        cfg = MagentConfig(projects=[], base_dir=None, settings=Settings())
        launch.revive_psmux(cfg, ["api"], "g")
        launch.revive_psmux(cfg, ["api"], "g", resume_parked=True)
        assert calls == [(["api"], "g", False), (["api"], "g", True)]

    def test_status_r_n_is_the_only_literal_true_in_the_product(self):
        root = Path(magent.__file__).parent
        hits: list[str] = []
        for py in sorted(root.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "resume_parked" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False
                    ):
                        hits.append(
                            f"{py.relative_to(root).as_posix()}:{ast.unparse(kw.value)}"
                        )
        # launch.revive_psmux forwards its own parameter; status's r<n> is the
        # one place a human asks for a specific pane back.
        assert hits == ["cli/status.py:True", "launch.py:resume_parked"]


class TestNothingHereReachesARealPsmux:
    def test_a_spawn_fails_the_test_before_the_os_sees_it(self, tmp_path):
        # A path that names no binary, so a broken guard fails on
        # FileNotFoundError instead of starting anything.
        argv = [str(tmp_path / "no-such-psmux.exe"), "ls"]
        with pytest.raises(pytest.fail.Exception, match="spawned a process"):
            subprocess.run(argv, check=False)

    def test_the_fleet_binary_resolves_to_nothing(self, monkeypatch, tmp_path):
        _Fleet(monkeypatch, tmp_path)
        binary = psmux.find_psmux()
        assert binary is not None
        assert Path(binary).is_absolute()  # a bare name would be a PATH lookup
        assert not Path(binary).exists()
        assert shutil.which(binary) is None
