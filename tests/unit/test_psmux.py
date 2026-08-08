"""Unit tests for magent.psmux leaf primitives.

Focused on pane_cwd's subprocess guards: the P1-06 extraction (137c8d5) that
moved the inline session_picker ``cwd()`` closure into psmux.pane_cwd dropped
its timeout=3 / encoding=utf-8 / errors=replace / OSError-swallow guards. These
pins restore and lock them so a psmux that hangs, emits non-utf-8 bytes, or
isn't launchable can never take down a caller (the attention picker fans
pane_cwd across every live session concurrently).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import unicodedata
from typing import ClassVar

import pytest

from magent import psmux
from magent.config import MagentConfig, ProjectConfig, Settings


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess with just the fields
    pane_cwd reads."""

    def __init__(self, returncode: int = 0, stdout: str | None = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class TestCapturePane:
    def test_returns_pane_text_with_guards(self, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="PS C:\\proj> claude\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert psmux.capture_pane("sess", psmux="psmux") == "PS C:\\proj> claude\n"
        assert captured["cmd"][:4] == ["psmux", "-L", "sess", "capture-pane"]
        assert captured["timeout"] == 3
        assert captured["errors"] == "replace"

    def test_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.capture_pane("sess", psmux="psmux") == ""


class TestPaneCwd:
    def test_targets_the_named_session_explicitly(self, monkeypatch):
        # Regression pin: without `-t <name>`, display-message answers for the
        # CALLING client's own pane. Verified live -- running the picker from
        # inside a psmux session reported the caller's cwd for 41 of 42
        # sessions, so every status row keyed off the wrong project.
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeCompleted(returncode=0, stdout="/home/proj\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert psmux.pane_cwd("sess", psmux="psmux") == "/home/proj"
        cmd = captured["cmd"]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_path}" in cmd

    def test_passes_timeout_encoding_and_errors_guards(self, monkeypatch):
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="/home/proj\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        result = psmux.pane_cwd("sess", psmux="psmux")

        assert result == "/home/proj"
        assert captured["timeout"] == 3
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"
        assert captured["check"] is False
        assert captured["capture_output"] is True

    def test_nonzero_returncode_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=1, stdout="ignored"),
        )
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    def test_none_stdout_is_guarded(self, monkeypatch):
        # `(result.stdout or "")` must survive a None stdout, not raise.
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=0, stdout=None),
        )
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("not launchable"),
            subprocess.TimeoutExpired(cmd="psmux", timeout=3),
        ],
    )
    def test_subprocess_failure_returns_empty(self, monkeypatch, exc):
        def _raise(cmd, **kwargs):
            raise exc

        monkeypatch.setattr(subprocess, "run", _raise)
        assert psmux.pane_cwd("sess", psmux="psmux") == ""

    def test_no_binary_returns_empty(self, monkeypatch):
        # No psmux passed and none on PATH -> "" without touching subprocess.
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_cwd("sess") == ""


class TestPaneCurrentCommand:
    def test_targets_the_named_session_explicitly(self, monkeypatch):
        # Regression pin: without `-t <name>`, display-message answers for the
        # CALLING client's own pane -- and magent commands are routinely run
        # from inside a psmux session, so revive would read the wrong pane.
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return _FakeCompleted(returncode=0, stdout="pwsh\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert psmux.pane_current_command("sess", psmux="psmux") == "pwsh"
        cmd = captured["cmd"]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_command}" in cmd
        assert captured["timeout"] == 3
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"
        assert captured["check"] is False

    def test_nonzero_returncode_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeCompleted(returncode=1, stdout="pwsh"),
        )
        assert psmux.pane_current_command("sess", psmux="psmux") == ""

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.pane_current_command("sess", psmux="psmux") == ""

    def test_no_binary_returns_empty(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_current_command("sess") == ""


class TestAgentIdle:
    @pytest.mark.parametrize(
        ("foreground", "idle"),
        [
            ("pwsh", True),
            ("pwsh.exe", True),
            ("PWSH.EXE", True),
            ("powershell", True),
            ("/usr/bin/bash", True),
            ("C:\\Program Files\\PowerShell\\7\\pwsh.exe", True),
            ("  pwsh  ", True),
            ("claude", False),
            ("PING", False),
            ("node", False),
            # Unreadable pane: never inject into a session we can't read.
            ("", False),
            # Deliberate exclusion (see _IDLE_SHELLS): on Windows the agent
            # launchers are .cmd shims, so cmd.exe is the foreground command
            # while an agent boots -- calling it idle would type a second
            # command into a live agent.
            ("cmd", False),
            ("cmd.exe", False),
        ],
    )
    def test_classification(self, monkeypatch, foreground, idle):
        monkeypatch.setattr(
            psmux, "pane_current_command", lambda name, psmux=None: foreground
        )
        assert psmux.agent_idle("sess", psmux="psmux") is idle


class _FakePopen:
    """Stand-in for one fanned-out `display-message` probe, logging when it is
    read so the spawn-then-read ordering can be pinned."""

    def __init__(self, name, stdout, returncode, events, timeout=False):
        self._name = name
        self._stdout = stdout
        self.returncode = returncode
        self._events = events
        self._timeout = timeout
        self.killed = False

    def communicate(self, timeout=None):
        self._events.append(f"read:{self._name}")
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="psmux", timeout=timeout or 0)
        return self._stdout, ""

    def kill(self):
        self.killed = True


class TestPaneCurrentCommands:
    def _fan(self, monkeypatch, results, timeouts=()):
        events: list[str] = []

        def _fake_popen(cmd, **kwargs):
            name = cmd[2]
            events.append(f"spawn:{name}")
            return _FakePopen(
                name,
                results.get(name, ("", 0))[0],
                results.get(name, ("", 0))[1],
                events,
                timeout=name in timeouts,
            )

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        return events

    def test_returns_one_reading_per_session(self, monkeypatch):
        self._fan(monkeypatch, {"a": ("claude\n", 0), "b": ("pwsh\n", 0)})
        assert psmux.pane_current_commands(["a", "b"], psmux="psmux") == {
            "a": "claude",
            "b": "pwsh",
        }

    def test_every_probe_is_spawned_before_any_is_read(self, monkeypatch):
        # The point of the fan-out: n concurrent psmux round-trips, not n
        # sequential ones -- `status` must stay fast at 40+ sessions.
        events = self._fan(
            monkeypatch, {"a": ("claude", 0), "b": ("claude", 0), "c": ("claude", 0)}
        )
        psmux.pane_current_commands(["a", "b", "c"], psmux="psmux")
        assert events == [
            "spawn:a",
            "spawn:b",
            "spawn:c",
            "read:a",
            "read:b",
            "read:c",
        ]

    def test_targets_each_session_explicitly(self, monkeypatch):
        argvs: list[list[str]] = []

        def _fake_popen(cmd, **kwargs):
            argvs.append(cmd)
            return _FakePopen(cmd[2], "claude", 0, [])

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        psmux.pane_current_commands(["sess"], psmux="psmux")
        cmd = argvs[0]
        assert cmd[:4] == ["psmux", "-L", "sess", "display-message"]
        assert cmd[cmd.index("-t") + 1] == "sess"
        assert "#{pane_current_command}" in cmd

    def test_nonzero_and_timeout_degrade_to_empty(self, monkeypatch):
        self._fan(monkeypatch, {"a": ("claude", 1), "b": ("x", 0)}, timeouts=("b",))
        assert psmux.pane_current_commands(["a", "b"], psmux="psmux") == {
            "a": "",
            "b": "",
        }

    def test_unlaunchable_probe_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, **kw: (_ for _ in ()).throw(OSError("no psmux")),
        )
        assert psmux.pane_current_commands(["a"], psmux="psmux") == {"a": ""}

    def test_no_binary_returns_empty_readings(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.pane_current_commands(["a", "b"]) == {"a": "", "b": ""}

    def test_no_names_spawns_nothing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda cmd, **kw: pytest.fail("spawned a probe for zero sessions"),
        )
        assert psmux.pane_current_commands([], psmux="psmux") == {}


class TestIsIdleCommand:
    """`agent_idle` now delegates here, so a caller that already holds the
    reading (status's session table) classifies it without a second probe."""

    @pytest.mark.parametrize(
        ("reading", "idle"),
        [("pwsh", True), ("C:\\x\\bash.exe", True), ("claude", False), ("", False)],
    )
    def test_classification_matches_agent_idle(self, reading, idle):
        assert psmux.is_idle_command(reading) is idle


def _cfg(projects, **settings):
    return MagentConfig(projects=projects, base_dir=None, settings=Settings(**settings))


class TestEligibleProjectsDedupe:
    def test_duplicate_project_yields_one_entry(self):
        # Two config entries for the same project used to produce two identical
        # status rows -> two identically-titled attach windows, the second of
        # which could never be tiled. First occurrence wins.
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude"),
                ProjectConfig(path="/b/api", tool="codex"),
            ]
        )
        out = psmux.eligible_projects(cfg)
        assert [p["session"] for p in out] == ["api"]
        assert out[0]["tool"] == "claude"
        assert out[0]["path"] == "/a/api"


class TestPsmuxStatusDownReason:
    """A project that is short-circuited to down -- never probed at all --
    carries WHY. The live case that motivated this: a project whose folder was
    deleted reported down forever and `up` silently skipped it, with no output
    anywhere naming the folder as the problem."""

    def _status(self, monkeypatch, cfg, *, binary="psmux"):
        monkeypatch.setattr(psmux, "find_psmux", lambda: binary)
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("probed a project that cannot be probed"),
        )
        _up, down, _all = psmux.psmux_status(cfg)
        return down

    def test_unresolved_folder_says_so(self, monkeypatch):
        cfg = _cfg([ProjectConfig(path="/nope/eBay", tool="claude")])
        down = self._status(monkeypatch, cfg)
        assert [d["name"] for d in down] == ["eBay"]
        assert down[0]["reason"] == "folder not found"

    def test_empty_command_says_so(self, monkeypatch, tmp_path):
        proj = tmp_path / "api"
        proj.mkdir()
        cfg = _cfg([ProjectConfig(path=str(proj), tool="claude")], tools={})
        down = self._status(monkeypatch, cfg)
        assert down[0]["reason"] == "no agent command"

    def test_missing_binary_wins_over_the_per_project_reason(
        self, monkeypatch, tmp_path
    ):
        # Precedence is binary-first: with no psmux installed nothing can be
        # brought up anyway, so naming the machine-wide blocker beats naming a
        # folder the user still could not launch.
        cfg = _cfg([ProjectConfig(path="/nope/eBay", tool="claude")])
        down = self._status(monkeypatch, cfg, binary=None)
        assert down[0]["reason"] == "psmux not installed"

    def test_a_probed_session_that_is_simply_down_carries_no_reason(
        self, monkeypatch, tmp_path
    ):
        # Ordinary down (has-session said no) is self-explanatory -- annotating
        # it would put "(...)" next to every session on a cold machine.
        proj = tmp_path / "api"
        proj.mkdir()
        cfg = _cfg([ProjectConfig(path=str(proj), tool="claude")])

        class _NoSession:
            def wait(self):
                return 1

        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _NoSession())
        _up, down, _all = psmux.psmux_status(cfg)
        assert [d["name"] for d in down] == ["api"]
        assert "reason" not in down[0]


class TestReviveSessions:
    """A psmux session whose agent was Ctrl-C'ed still answers `has-session`,
    so up/attach reuse it and hand back a window at a bare prompt. Revive
    re-sends the agent command to exactly those panes."""

    def _run(self, monkeypatch, *, idle, only=None, sent_ok=True):
        sent: list[tuple] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "has_session", lambda name, psmux=None: True)
        monkeypatch.setattr(psmux, "agent_idle", lambda name, psmux=None: name in idle)

        def _fake_send(name, *keys, target=None, psmux=None):
            sent.append((name, keys, target))
            return sent_ok

        monkeypatch.setattr(psmux, "send_keys", _fake_send)
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude"),
                ProjectConfig(path="/a/web", tool="claude"),
            ]
        )
        return psmux.revive_sessions(cfg, only=only), sent

    def test_only_the_idle_pane_is_revived(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle={"api"})
        assert revived == ["api"]
        assert [s[0] for s in sent] == ["api"]

    def test_sends_the_agents_resume_command_and_enter(self, monkeypatch):
        # claude's registry default is `claude --continue`, which picks the
        # dead pane's conversation back up rather than starting a fresh chat.
        _, sent = self._run(monkeypatch, idle={"api"})
        name, keys, target = sent[0]
        assert "claude --continue" in keys[0]
        assert keys[-1] == "Enter"
        # -t is required: send-keys without it can land in the caller's pane.
        assert target == name

    def test_busy_pane_is_left_alone(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle=set())
        assert revived == []
        assert sent == []

    def test_only_filter_restricts_candidates(self, monkeypatch):
        revived, sent = self._run(monkeypatch, idle={"api", "web"}, only=["web"])
        assert revived == ["web"]
        assert [s[0] for s in sent] == ["web"]

    def test_failed_send_is_not_reported_as_revived(self, monkeypatch):
        revived, _ = self._run(monkeypatch, idle={"api"}, sent_ok=False)
        assert revived == []

    def test_no_psmux_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        cfg = _cfg([ProjectConfig(path="/a/api", tool="claude")])
        assert psmux.revive_sessions(cfg) == []


class TestHasSessionTimeout:
    """``has_session`` gained a bounded form for the bring-up creation verify:
    a wedged psmux server answers nothing at all (the MCPAIRelease incident had
    every control command against its socket time out at 3s), and an unbounded
    probe would hang the whole verify behind it."""

    def _probe(self, monkeypatch, *, run):
        monkeypatch.setattr(subprocess, "run", run)
        return psmux.has_session("api", psmux="psmux", timeout=3.0)

    def test_a_timed_out_probe_is_not_alive(self, monkeypatch):
        def _timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 3.0))

        assert self._probe(monkeypatch, run=_timeout) is False

    def test_the_bound_is_handed_to_subprocess(self, monkeypatch):
        seen: list[object] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _FakeCompleted(returncode=0)

        assert self._probe(monkeypatch, run=_run) is True
        assert seen == [3.0]

    def test_the_default_call_stays_unbounded(self, monkeypatch):
        # Back-compat pin: psmux_status/revive callers must keep their existing
        # blocking behaviour -- only the verify opts into a bound.
        seen: list[object] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _FakeCompleted(returncode=1)

        monkeypatch.setattr(subprocess, "run", _run)
        assert psmux.has_session("api", psmux="psmux") is False
        assert seen == [None]


class TestHasSessionTargetsTheSession:
    """A BARE ``has-session`` is not a liveness probe.

    Proven live on psmux 3.3.6: ``psmux -L definitely-not-a-session-xyz
    has-session`` exits 0 -- there is no server on that socket at all, and psmux
    keeps internal ``__warm__`` spares that answer anyway. Every probe in the
    product therefore reported UP for dead sessions (status said 42/42 up where
    ``-t`` correctly said 40 up / 2 down), which blinded the menu's "already
    running", the creation verify, revive and the corpse sweeps at once.
    """

    def _argv(self, monkeypatch, **kwargs):
        seen: list[list[str]] = []

        def _run(cmd, **_kw):
            seen.append(list(cmd))
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(subprocess, "run", _run)
        psmux.has_session("api", psmux="psmux", **kwargs)
        return seen[0]

    def test_the_probe_names_the_session_with_t(self, monkeypatch):
        assert self._argv(monkeypatch) == [
            "psmux",
            "-L",
            "api",
            "has-session",
            "-t",
            "api",
        ]

    def test_the_bounded_form_targets_it_too(self, monkeypatch):
        # The creation verify uses the bounded call; both forms are one argv.
        assert self._argv(monkeypatch, timeout=3.0)[-2:] == ["-t", "api"]

    def test_the_status_fan_out_targets_each_session(self, monkeypatch, tmp_path):
        argvs: list[list[str]] = []

        class _Proc:
            def wait(self):
                return 0

        def _popen(cmd, **_kw):
            argvs.append(list(cmd))
            return _Proc()

        (tmp_path / "api").mkdir()
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(subprocess, "Popen", _popen)
        psmux.psmux_status(_cfg([ProjectConfig(path=str(tmp_path / "api"))]))
        assert argvs == [["psmux", "-L", "api", "has-session", "-t", "api"]]


class TestChildrenRunWithoutNestingMarkers:
    """Every psmux CREATION/CONTROL/PROBE child runs with PSMUX_*/TMUX_*
    stripped (see env.psmux_child_env). The bug this closes: running the menu
    from inside a magent psmux window made psmux refuse the sibling session it
    was asked to create -- "sessions should be nested with care, unset
    PSMUX_SESSION to force" -- while still exiting 0."""

    @pytest.fixture(autouse=True)
    def _inside_a_session(self, monkeypatch):
        monkeypatch.setenv("PSMUX_SESSION", "api")
        monkeypatch.setenv("TMUX", "/tmp/psmux-1/default,123,0")
        monkeypatch.setenv("TMUX_PANE", "%1")

    def _env_of(self, monkeypatch, call):
        seen: list[object] = []

        def _record(cmd, **kwargs):
            seen.append(kwargs.get("env"))
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", _record)
        call()
        assert seen, "nothing was spawned"
        return seen

    @pytest.mark.parametrize(
        "call",
        [
            lambda: psmux.has_session("api", psmux="psmux"),
            lambda: psmux.kill_server("api", psmux="psmux"),
            lambda: psmux.send_keys("api", "claude", "Enter", psmux="psmux"),
            lambda: psmux.pane_cwd("api", psmux="psmux"),
            lambda: psmux.capture_pane("api", psmux="psmux"),
            lambda: psmux.pane_current_command("api", psmux="psmux"),
            lambda: psmux.detach_client("api", psmux="psmux"),
            lambda: psmux.flash_message("api", "hi", 100, psmux="psmux"),
        ],
    )
    def test_the_markers_never_reach_the_child(self, monkeypatch, call):
        for env in self._env_of(monkeypatch, call):
            assert isinstance(env, dict)
            assert not [k for k in env if k.upper().startswith(("PSMUX", "TMUX"))]

    def test_the_decoration_pass_is_cleaned_too(self, monkeypatch):
        monkeypatch.setattr(psmux, "code_on_path", lambda: True)
        for env in self._env_of(
            monkeypatch, lambda: psmux.decorate_session("api", psmux="psmux")
        ):
            assert not [k for k in env if k.upper().startswith(("PSMUX", "TMUX"))]

    def test_the_pane_command_fan_out_is_cleaned_too(self, monkeypatch):
        seen: list[object] = []

        class _Proc:
            returncode = 0

            def communicate(self, timeout=None):
                return "claude", ""

        def _popen(cmd, **kwargs):
            seen.append(kwargs.get("env"))
            return _Proc()

        monkeypatch.setattr(subprocess, "Popen", _popen)
        psmux.pane_current_commands(["api"], psmux="psmux")
        assert seen and all(
            not [k for k in env if k.upper().startswith(("PSMUX", "TMUX"))]
            for env in seen
        )

    def test_the_rest_of_the_environment_is_preserved(self, monkeypatch):
        monkeypatch.setenv("MDTEST_KEEP", "yes")
        envs = self._env_of(
            monkeypatch, lambda: psmux.has_session("api", psmux="psmux")
        )
        assert envs[0]["MDTEST_KEEP"] == "yes"


class TestBringUpCreationVerify:
    """Session CREATION is verified, and one bounded respawn is attempted.

    Motivating incident: during a ~40-session attach bring-up storm one
    project's psmux server wedged -- every control command against its socket
    timed out -- and nothing detected it. ``launch_psmux_session`` was called
    once and never probed afterwards, so the picker showed the project "down"
    forever; only a second, storm-free ``magent attach`` brought it back.
    """

    @pytest.fixture
    def slept(self, monkeypatch):
        """The settle before each probe is real seconds; nothing here waits on
        a real process, so record the pauses instead of taking them."""
        out: list[float] = []
        monkeypatch.setattr("magent.psmux.time.sleep", out.append)
        return out

    def _bring_up(self, monkeypatch, tmp_path, *, names, failures=(), plat=None):
        from tests.conftest import FakePlatform

        projects = []
        for n in names:
            (tmp_path / n).mkdir()
            projects.append(ProjectConfig(path=str(tmp_path / n), tool="claude"))
        fp = plat or FakePlatform(
            supports_psmux=True, psmux_launch_failures=set(failures)
        )
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(
            psmux,
            "has_session",
            lambda name, psmux=None, timeout=None: name in fp.psmux_sessions,
        )
        return psmux.bring_up(_cfg(projects)), fp

    def test_a_session_that_never_came_up_is_respawned(
        self, monkeypatch, tmp_path, slept
    ):
        _created, fp = self._bring_up(
            monkeypatch, tmp_path, names=["api", "web"], failures=["api"]
        )
        assert fp.psmux_launches == [["api", "web"], ["api"]]
        assert fp.psmux_sessions == {"api", "web"}

    def test_only_the_missing_subset_is_respawned(self, monkeypatch, tmp_path, slept):
        _created, fp = self._bring_up(
            monkeypatch, tmp_path, names=["api", "web", "docs"], failures=["web"]
        )
        assert fp.psmux_launches[1] == ["web"]

    def test_a_healthy_bring_up_is_never_respawned(self, monkeypatch, tmp_path, slept):
        _created, fp = self._bring_up(monkeypatch, tmp_path, names=["api", "web"])
        assert fp.psmux_launches == [["api", "web"]]

    def test_the_respawn_replays_the_full_launch_path(
        self, monkeypatch, tmp_path, slept
    ):
        # The retry goes back through `plat.launch_psmux_session` on purpose --
        # a hand-rolled `new-session` would skip the send-keys verify, the
        # status-line decoration and the batch pacing the original recipe has.
        _created, fp = self._bring_up(
            monkeypatch, tmp_path, names=["api"], failures=["api"]
        )
        retried = fp.launched_psmux[-1]
        assert retried.window_name == "api"
        assert retried.command == "claude --continue"
        assert retried.cwd == str(tmp_path / "api")

    def test_a_recovered_session_counts_as_created(self, monkeypatch, tmp_path, slept):
        # `api` fails its first launch and comes up on the respawn, so the wave
        # really did bring up both -- nothing is reported failed.
        (created, failed), _fp = self._bring_up(
            monkeypatch, tmp_path, names=["api", "web"], failures=["api"]
        )
        assert created == ["api", "web"]
        assert failed == []

    def test_the_probe_gets_a_settle_before_it_runs(self, monkeypatch, tmp_path, slept):
        # Probing at t=0 would misclassify a slow-but-fine server on a loaded
        # host -- the storm's timeouts were transient churn.
        self._bring_up(monkeypatch, tmp_path, names=["api"])
        assert slept == [psmux._CREATE_VERIFY_SETTLE_S]

    def test_the_re_probe_settles_too(self, monkeypatch, tmp_path, slept):
        self._bring_up(monkeypatch, tmp_path, names=["api"], failures=["api"])
        assert slept == [psmux._CREATE_VERIFY_SETTLE_S] * 2

    def test_the_probe_is_bounded_so_a_wedge_cannot_stall_the_verify(
        self, monkeypatch, tmp_path, slept
    ):
        from tests.conftest import FakePlatform

        seen: list[object] = []
        (tmp_path / "api").mkdir()
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

        def _probe(name, psmux=None, timeout=None):
            seen.append(timeout)
            return True

        monkeypatch.setattr(psmux, "has_session", _probe)
        psmux.bring_up(_cfg([ProjectConfig(path=str(tmp_path / "api"), tool="claude")]))
        assert seen and all(t is not None for t in seen)

    def test_a_wedged_server_counts_as_missing_and_is_retried(
        self, monkeypatch, tmp_path, slept, caplog
    ):
        # ASYMMETRY with the send-keys verifier, deliberately: that one leaves
        # a pane whose state it could not read ALONE (re-sending would type
        # into a live agent). Creation has no such hazard -- `new-session` is a
        # no-op against a session that already exists -- so an unreadable or
        # timed-out probe counts as MISSING here.
        from tests.conftest import FakePlatform

        (tmp_path / "api").mkdir()
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(
            psmux, "has_session", lambda name, psmux=None, timeout=None: False
        )
        with caplog.at_level(logging.WARNING, logger="magent.launch"):
            psmux.bring_up(
                _cfg([ProjectConfig(path=str(tmp_path / "api"), tool="claude")])
            )
        assert fp.psmux_launches == [["api"], ["api"]]

    def test_the_warning_names_the_sessions_that_did_not_come_up(
        self, monkeypatch, tmp_path, slept, caplog
    ):
        with caplog.at_level(logging.WARNING, logger="magent.launch"):
            self._bring_up(
                monkeypatch, tmp_path, names=["api", "web"], failures=["api"]
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "a session that never came up must be logged loudly"
        text = " ".join(r.getMessage() for r in warnings)
        assert "session did not come up after bring-up" in text
        assert "api" in text
        assert "web" not in text  # the healthy one is not slandered

    def test_a_session_missing_after_the_respawn_is_logged_and_left_alone(
        self, monkeypatch, tmp_path, slept, caplog
    ):
        from tests.conftest import FakePlatform

        (tmp_path / "api").mkdir()
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(
            psmux, "has_session", lambda name, psmux=None, timeout=None: False
        )
        with caplog.at_level(logging.WARNING, logger="magent.launch"):
            created = psmux.bring_up(
                _cfg([ProjectConfig(path=str(tmp_path / "api"), tool="claude")])
            )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and "api" in errors[0].getMessage()
        # Exactly one retry: the wave must not be spent on one stuck session.
        assert len(fp.psmux_launches) == 2
        # ...and the casualty is REPORTED, not counted as a success. `bring_up`
        # used to discard the verify's answer and return every attempted name,
        # so the caller printed "Brought up 1 session(s)" for a session the log
        # in the very same run called "never came up".
        assert created == ([], ["api"])

    def test_a_respawn_that_cannot_be_launched_never_raises(
        self, monkeypatch, tmp_path, slept, caplog
    ):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_psmux=True, psmux_launch_failures={"api"})
        original = fp.launch_psmux_session
        calls: list[int] = []

        def _flaky(windows):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("psmux vanished mid-wave")
            original(windows)

        fp.launch_psmux_session = _flaky
        with caplog.at_level(logging.WARNING, logger="magent.launch"):
            created, _fp = self._bring_up(
                monkeypatch, tmp_path, names=["api"], failures=["api"], plat=fp
            )
        assert created == ([], ["api"])
        assert len(calls) == 2

    def test_no_psmux_binary_skips_the_verify_entirely(
        self, monkeypatch, tmp_path, slept
    ):
        from tests.conftest import FakePlatform

        (tmp_path / "api").mkdir()
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(
            psmux,
            "has_session",
            lambda *a, **k: pytest.fail("probed with no psmux binary"),
        )
        created = psmux.bring_up(
            _cfg([ProjectConfig(path=str(tmp_path / "api"), tool="claude")])
        )
        assert fp.psmux_launches == [["api"]]
        assert slept == []
        # Unprovable is not "fine": with no binary to probe with, nothing may be
        # claimed as created either.
        assert created == ([], ["api"])


class TestBringUpContainsCreationFailures:
    """A window psmux refuses must cost only itself.

    Live repro of the user's "error towards the end": one project's
    ``new-session`` exited 1 (``psmux: failed to create session 'EmailSESFix'``)
    and ``platform/windows.py`` raised ``CalledProcessError`` through
    ``launch_verified``'s FIRST, unguarded ``launch_psmux_session`` call -- a
    traceback out of `magent up` and out of the menu's `u`, with every remaining
    session in the wave abandoned.
    """

    @pytest.fixture
    def slept(self, monkeypatch):
        out: list[float] = []
        monkeypatch.setattr("magent.psmux.time.sleep", out.append)
        return out

    def _plat(self, monkeypatch, *, boom, live):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_psmux=True)
        calls: list[list[str]] = []

        def _launch(windows):
            calls.append([w.window_name for w in windows])
            if len(calls) == 1:
                raise boom
            for w in windows:
                fp.psmux_sessions.add(w.window_name)

        fp.launch_psmux_session = _launch
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr(
            psmux,
            "has_session",
            lambda name, psmux=None, timeout=None: (
                name in live or name in fp.psmux_sessions
            ),
        )
        return fp, calls

    def _windows(self, names):
        return [
            psmux.PsmuxWindowOpts(window_name=n, cwd=f"/a/{n}", command="claude")
            for n in names
        ]

    def test_a_raising_first_launch_does_not_propagate(self, monkeypatch, slept):
        boom = subprocess.CalledProcessError(1, ["psmux", "new-session"])
        fp, calls = self._plat(monkeypatch, boom=boom, live={"web"})
        # No pytest.raises: the point is that nothing escapes.
        failed = psmux.launch_verified(fp, self._windows(["api", "web"]))
        assert calls[0] == ["api", "web"]
        assert failed == []

    def test_the_verify_still_runs_and_respawns_the_missing(self, monkeypatch, slept):
        boom = subprocess.CalledProcessError(1, ["psmux", "new-session"])
        fp, calls = self._plat(monkeypatch, boom=boom, live={"web"})
        psmux.launch_verified(fp, self._windows(["api", "web"]))
        # `web` was already live; only `api` is respawned -- the wave's other
        # sessions are not re-created, and none of them were abandoned.
        assert calls[1] == ["api"]

    def test_the_raise_is_logged_with_its_traceback(self, monkeypatch, slept, caplog):
        boom = OSError("psmux vanished")
        fp, _calls = self._plat(monkeypatch, boom=boom, live={"api"})
        with caplog.at_level(logging.ERROR, logger="magent.launch"):
            psmux.launch_verified(fp, self._windows(["api"]))
        assert any("bring-up raised" in r.getMessage() for r in caplog.records)

    def test_a_session_that_stays_down_is_returned_as_a_casualty(
        self, monkeypatch, slept
    ):
        # First launch raises, the respawn is a silent no-op, the session never
        # answers: it is REPORTED, never claimed as created.
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_psmux=True)
        calls: list[int] = []

        def _launch(windows):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, ["psmux", "new-session"])

        fp.launch_psmux_session = _launch
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "has_session", lambda name, psmux=None, timeout=None: False
        )
        assert psmux.launch_verified(fp, self._windows(["api"])) == ["api"]
        assert len(calls) == 2


# The status-right hint, restated here on purpose: an independent copy is what
# makes these pins catch a drive-by restyle instead of following it.
_EXPECTED_HINT = (
    "#[bold,fg=cyan] F1 #[default]Proj. Picker   "
    "#[bold,fg=cyan] F2 #[default]</> VS Code "
)

# ...and what a machine with no VS Code gets instead: the F1 half alone. F2 is
# the hotkey listener's, and the listener needs `code` on PATH -- advertising
# it on a box without VS Code is a lie the user can only discover by pressing
# the key. Restated here for the same reason as the full hint: a restyle in
# psmux.py must be a deliberate edit here too.
_EXPECTED_HINT_F1_ONLY = "#[bold,fg=cyan] F1 #[default]Proj. Picker "


def _visible_cells(status: str) -> int:
    """Worst-case columns `status` occupies. tmux style directives are free.

    Wide and fullwidth characters cost 2 cells; so does an *ambiguous*-width
    one, which a terminal may render either way -- the budget is only safe if
    it assumes the wide rendering. Today's hint is pure ASCII (pinned below),
    so the 2-cell term contributes nothing; it stays so the budget check keeps
    telling the truth if a wide glyph ever sneaks back in.
    """
    text = re.sub(r"#\[[^\]]*\]", "", status)
    return sum(
        2 if unicodedata.east_asian_width(ch) in {"W", "F", "A"} else 1 for ch in text
    )


class TestDecorateSession:
    """The status-line hints (badged `F1`/`F2` keys with spelled-out labels),
    the product-owned `bind -n F1 detach-client`, and the product-owned
    status-left brand -- each half paired with the length budget it needs. All
    are `-L <name>`-scoped so they land on that session's own server and beat
    whatever its tmux.conf set at start-up.
    """

    def _run(
        self, monkeypatch, *, boom: Exception | None = None, code_hint: bool = True
    ):
        cmds: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            cmds.append(cmd)
            if boom is not None:
                raise boom
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        # Pinned, never probed: the dev box and the CI runners disagree about
        # whether `code` is on PATH, and a hint pin that reads the ambient
        # PATH pins nothing.
        monkeypatch.setattr(psmux, "code_on_path", lambda: code_hint)
        psmux.decorate_session("api", psmux="psmux")
        return cmds

    def test_binds_f1_to_detach_client(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[0] == ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"]

    def test_sets_the_status_right_hint(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[1] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right",
            _EXPECTED_HINT,
        ]

    def test_sets_the_status_right_length_alongside_the_hint(self, monkeypatch):
        # Same load-bearing pairing as the brand below: the badged hint is far
        # wider than the old text, and a personal tmux.conf with a tighter
        # status-right-length would cut it mid-label.
        cmds = self._run(monkeypatch)
        assert cmds[2] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right-length",
            "40",
        ]

    def test_sets_the_status_left_brand(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[3] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left",
            "#[bold,fg=green] magent #[default]",
        ]

    def test_sets_the_status_left_length_alongside_the_brand(self, monkeypatch):
        # Load-bearing: a personal tmux.conf with a tighter status-left-length
        # would truncate the brand mid-word, so magent sets both or neither.
        cmds = self._run(monkeypatch)
        assert cmds[4] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left-length",
            "10",
        ]

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_the_argv_is_the_six_decorations(self, code_hint):
        # The list is what every call site fans out, so its shape is contract:
        # a seventh command (or a dropped one) has to be a deliberate edit here.
        # Gating F2 changes the hint TEXT and the LAST command's form, never the
        # four `set`s -- dropping one of those would leave a personal tmux.conf's
        # value in place.
        argv = psmux.decoration_argv("api", "psmux", code_hint)
        assert len(argv) == 6
        assert argv[0][3:] == ["bind", "-n", "F1", "detach-client"]
        assert [cmd[5] for cmd in argv[1:5]] == [
            "status-right",
            "status-right-length",
            "status-left",
            "status-left-length",
        ]
        assert argv[5][3] in {"bind", "unbind-key"}

    def test_status_left_length_fits_the_brand(self):
        # The number is only correct relative to the brand text; pin the
        # relationship, not just the two literals.
        assert int(psmux._STATUS_BRAND_LEN) >= len(" magent ")

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_status_right_length_fits_the_hint(self, code_hint):
        # Same relationship on the other half. Style directives are free; count
        # cells, not characters, so a future wide glyph cannot shrink the check.
        # Both variants: the F1-only budget is a smaller number guarding a
        # shorter string, and it has to keep fitting it.
        hint, budget = psmux.status_hints(code_hint)
        assert int(budget) >= _visible_cells(hint)

    @pytest.mark.parametrize("code_hint", [True, False])
    def test_hint_is_pure_ascii(self, code_hint):
        # The load-bearing invariant behind the 3.10.3 hotfix: an East-Asian
        # AMBIGUOUS-width glyph (the U+2630 menu hamburger) made psmux and
        # Windows Terminal disagree on cell arithmetic -- a stray highlighted
        # cell inside the bar and a wrapped phantom row under it. A status bar
        # needs the renderer and the multiplexer to agree on width, so the hint
        # stays ASCII-only. This must fail before any glyph goes back in --
        # for every variant, and for each half on its own, so a glyph cannot
        # sneak into the half the default probe happens not to emit.
        assert psmux.status_hints(code_hint)[0].isascii()
        assert psmux._STATUS_HINTS_F1.isascii()
        assert psmux._STATUS_HINTS_F2.isascii()
        assert psmux._STATUS_HINTS.isascii()

    def test_hint_advertises_both_keys(self):
        # The literal is defined once; a rename must not silently drop a key.
        argv = psmux.decoration_argv("api", "psmux", True)
        assert "F1" in argv[1][-1] and "F2" in argv[1][-1]

    def test_hint_badges_each_key_name(self):
        # The readability fix: each key name is its own bold accent badge, so
        # "F1"/"F2" can't read as words in the label next to them.
        hint = psmux.decoration_argv("api", "psmux", True)[1][-1]
        assert "#[bold,fg=cyan] F1 #[default]" in hint
        assert "#[bold,fg=cyan] F2 #[default]" in hint

    def test_hint_spells_out_what_each_key_does(self):
        # The other half of the fix: " F1 picker  F2 code " told the user
        # nothing. The labels, not the styling, are what must survive.
        hint = psmux.decoration_argv("api", "psmux", True)[1][-1]
        assert "Proj. Picker" in hint
        assert "VS Code" in hint
        assert "</> VS Code" in hint

    def test_brand_names_the_product(self):
        # Same guard on the other literal: the status-left is branding, so the
        # product name is the part that must survive a restyle.
        argv = psmux.decoration_argv("api", "psmux", True)
        assert "magent" in argv[3][-1]

    def test_the_full_hint_is_the_two_halves_with_a_three_column_seam(self):
        # The split must not change what a VS-Code machine renders: the seam
        # is still exactly three columns wide, as it was when the hint was one
        # literal. Off-by-one here is invisible in review and obvious on screen.
        assert psmux._STATUS_HINTS == _EXPECTED_HINT
        assert psmux._STATUS_HINTS == (
            _EXPECTED_HINT_F1_ONLY + psmux._STATUS_HINTS_GAP + psmux._STATUS_HINTS_F2
        )
        # One trailing space on the F1 half + the two-space seam = 3 columns.
        assert psmux._STATUS_HINTS_F1.endswith(" ")
        assert not psmux._STATUS_HINTS_F1.endswith("  ")
        assert psmux._STATUS_HINTS_GAP == "  "

    def test_with_code_the_hint_is_the_full_text_and_budget(self, monkeypatch):
        cmds = self._run(monkeypatch, code_hint=True)
        assert cmds[1][-1] == _EXPECTED_HINT
        assert cmds[2][-1] == "40"

    def test_without_code_the_f2_half_is_gone_from_every_argv(self, monkeypatch):
        # The user-visible promise: on a machine with no VS Code, nothing magent
        # sends ADVERTISES the key it cannot honour. The one remaining mention
        # is the `unbind-key -n F2` that retracts a stale binding from back when
        # `code` did resolve here -- checked separately below.
        cmds = self._run(monkeypatch, code_hint=False)
        assert cmds[1][-1] == _EXPECTED_HINT_F1_ONLY
        assert cmds[2][-1] == psmux._STATUS_HINTS_F1_LEN
        flat = " ".join(arg for cmd in cmds[:5] for arg in cmd)
        assert "</>" not in flat
        assert "VS Code" not in flat
        assert "F2" not in flat
        assert cmds[5] == ["psmux", "-L", "api", "unbind-key", "-n", "F2"]

    def test_with_code_f2_falls_back_to_an_explanatory_message(self, monkeypatch):
        # The Termius story: a viewer with no magent hotkey listener would
        # otherwise lose F2 into the pane while the bar still advertises it.
        cmds = self._run(monkeypatch, code_hint=True)
        assert cmds[5][:6] == ["psmux", "-L", "api", "bind", "-n", "F2"]
        assert cmds[5][6] == "display-message"
        assert cmds[5][7] == psmux._F2_FALLBACK_MSG

    def test_the_f2_fallback_message_is_pure_ascii(self):
        # Same load-bearing invariant as the hint's: this text renders in the
        # status bar via display-message, and an ambiguous-width glyph there
        # desyncs psmux's and Windows Terminal's cell arithmetic.
        msg = psmux.decoration_argv("api", "psmux", True)[5][-1]
        assert all(ord(c) < 128 for c in msg)
        assert msg.isascii()
        assert "\n" not in msg

    def test_the_f2_fallback_message_explains_the_situation(self):
        # A message that just said "F2" would be no better than silence.
        msg = psmux._F2_FALLBACK_MSG
        assert "F2" in msg
        assert "VS Code" in msg
        assert "listener" in msg

    def test_without_code_the_budget_is_smaller_than_the_full_one(self):
        # Not just "some number": dropping half the text without dropping the
        # budget would leave the constant documenting a string it no longer
        # guards.
        assert int(psmux._STATUS_HINTS_F1_LEN) < int(psmux._STATUS_HINTS_LEN)
        assert int(psmux._STATUS_HINTS_F1_LEN) >= _visible_cells(psmux._STATUS_HINTS_F1)

    def test_f1_is_bound_either_way(self, monkeypatch):
        # F1 is magent's own psmux binding, installed right here -- it is true
        # for any viewer of the session, so no probe may gate it.
        for hint in (True, False):
            cmds = self._run(monkeypatch, code_hint=hint)
            assert cmds[0] == [
                "psmux",
                "-L",
                "api",
                "bind",
                "-n",
                "F1",
                "detach-client",
            ]

    def test_code_hint_defaults_to_probing_this_machine(self, monkeypatch):
        # `code_hint=None` means "ask here" -- the decorating machine is the
        # one whose PATH decides.
        cmds: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **k: (
                cmds.append(cmd),
                _FakeCompleted(returncode=0, stdout=""),
            )[1],
        )
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        psmux.decorate_session("api", psmux="psmux")
        assert cmds[1][-1] == _EXPECTED_HINT_F1_ONLY

    def test_code_on_path_asks_shutil_for_code(self, monkeypatch):
        # The one owner of the probe; hotkey.py::_do_open_code resolves the
        # very same name, and the hint must not promise a different binary.
        asked: list[str] = []
        monkeypatch.setattr(
            psmux.shutil, "which", lambda n: asked.append(n) or "/usr/bin/code"
        )
        assert psmux.code_on_path() is True
        assert asked == ["code"]
        monkeypatch.setattr(psmux.shutil, "which", lambda n: None)
        assert psmux.code_on_path() is False

    def test_never_raises_when_the_subprocess_fails(self, monkeypatch):
        # A status bar is cosmetic: an unlaunchable/hung psmux is logged and
        # swallowed, never propagated into a bring-up.
        cmds = self._run(monkeypatch, boom=OSError("no psmux"))
        assert len(cmds) == 6  # all attempted; none escaped

    def test_never_raises_on_timeout(self, monkeypatch):
        self._run(monkeypatch, boom=subprocess.TimeoutExpired("psmux", 3))

    def test_no_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)

        def _boom(*a, **k):
            raise AssertionError("must not shell out without a psmux binary")

        monkeypatch.setattr(subprocess, "run", _boom)
        psmux.decorate_session("api")

    def test_fan_out_covers_every_name(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "code_on_path", lambda: True)
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: seen.append(n),
        )
        assert psmux.decorate_sessions(["api", "web"]) == ["api", "web"]
        assert sorted(seen) == ["api", "web"]

    def test_fan_out_probes_for_code_exactly_once(self, monkeypatch):
        # The answer is a property of the machine, not of a session: probing
        # per name would be one filesystem sweep each for one shared answer.
        probes: list[int] = []
        hints: list[bool | None] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "code_on_path", lambda: (probes.append(1), True)[1])
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: hints.append(code_hint),
        )
        psmux.decorate_sessions(["api", "web", "docs"])
        assert len(probes) == 1
        assert hints == [True, True, True]

    def test_fan_out_passes_an_explicit_hint_through_without_probing(self, monkeypatch):
        hints: list[bool | None] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")

        def _boom():
            raise AssertionError("must not probe when the caller already knows")

        monkeypatch.setattr(psmux, "code_on_path", _boom)
        monkeypatch.setattr(
            psmux,
            "decorate_session",
            lambda n, psmux=None, code_hint=None: hints.append(code_hint),
        )
        psmux.decorate_sessions(["api", "web"], code_hint=False)
        assert hints == [False, False]

    def test_fan_out_without_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.decorate_sessions(["api"]) == []


class _SpawnRecorder:
    """Records what was spawned and screams if anyone waits on it."""

    spawned: ClassVar[list[tuple[list[str], dict[str, object]]]] = []

    def __init__(self, cmd, **kwargs):
        type(self).spawned.append((cmd, kwargs))
        self.returncode = None

    def wait(self, timeout=None):
        raise AssertionError("the status path must never wait on decoration")

    def communicate(self, *args, **kwargs):
        raise AssertionError("the status path must never wait on decoration")


class TestDecorateSessionsAsync:
    """The status-path variant: `up --json` (the host side of `magent attach`)
    must never wait on a cosmetic status bar. The synchronous fan-out runs each
    session's commands under a 3s-timeout `subprocess.run`, so a busy host paid
    ~15s per session before printing any JSON -- past the attach client's 30s
    timeout, which then retried with a 120s one.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        # Never touch the real ~/.magent/state/decor.stamp from a unit test.
        monkeypatch.setattr(psmux, "DECOR_STAMP", tmp_path / "decor.stamp")
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "code_on_path", lambda: True)
        _SpawnRecorder.spawned = []
        monkeypatch.setattr(subprocess, "Popen", _SpawnRecorder)

    def test_fires_every_argv_for_every_session(self):
        assert psmux.decorate_sessions_async(["api", "web"]) == ["api", "web"]
        cmds = [cmd for cmd, _ in _SpawnRecorder.spawned]
        assert len(cmds) == 2 * len(psmux.decoration_argv("api", "psmux", True))
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] in cmds
        assert ["psmux", "-L", "web", "bind", "-n", "F1", "detach-client"] in cmds

    def test_never_waits_and_detaches_every_stdio_handle(self):
        # A wait here is the whole bug; DEVNULL on all three keeps a decoration
        # from writing into the JSON the caller is about to print, or from
        # blocking on a pipe nobody drains.
        psmux.decorate_sessions_async(["api"])
        assert _SpawnRecorder.spawned
        for _cmd, kwargs in _SpawnRecorder.spawned:
            assert kwargs["stdin"] is subprocess.DEVNULL
            assert kwargs["stdout"] is subprocess.DEVNULL
            assert kwargs["stderr"] is subprocess.DEVNULL
        # _SpawnRecorder.wait() raises, so a wait anywhere above would have failed.

    def test_never_runs_the_blocking_subprocess_run(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the status path must not block on subprocess.run")
            ),
        )
        psmux.decorate_sessions_async(["api"])

    def test_a_second_call_inside_the_ttl_fires_nothing(self):
        psmux.decorate_sessions_async(["api"])
        fired = len(_SpawnRecorder.spawned)
        assert fired > 0
        # attach polls `up --json` up to ~20 times per bring-up; without the
        # throttle each poll would spawn another full wave.
        assert psmux.decorate_sessions_async(["api"]) == []
        assert len(_SpawnRecorder.spawned) == fired

    def test_a_stale_stamp_fires_again(self, monkeypatch):
        psmux.decorate_sessions_async(["api"])
        fired = len(_SpawnRecorder.spawned)
        stale = time.time() - (psmux.DECOR_TTL_S + 1)
        os.utime(psmux.DECOR_STAMP, (stale, stale))
        assert psmux.decorate_sessions_async(["api"]) == ["api"]
        assert len(_SpawnRecorder.spawned) > fired

    def test_the_first_call_stamps(self):
        assert not psmux.DECOR_STAMP.exists()
        psmux.decorate_sessions_async(["api"])
        assert psmux.DECOR_STAMP.is_file()

    def test_a_future_dated_stamp_is_not_treated_as_fresh(self, monkeypatch):
        psmux.decorate_sessions_async(["api"])
        ahead = time.time() + 10 * psmux.DECOR_TTL_S
        os.utime(psmux.DECOR_STAMP, (ahead, ahead))
        # A bad clock must not be able to switch decoration off indefinitely.
        assert psmux.decorate_sessions_async(["api"]) == ["api"]

    def test_an_unstampable_home_still_decorates(self, monkeypatch):
        # Read-only home: the throttle degrades to "always fire", never to an
        # error out of a status query.
        def _boom(*a, **k):
            raise OSError("read-only")

        monkeypatch.setattr(psmux.Path, "mkdir", _boom)
        assert psmux.decorate_sessions_async(["api"]) == ["api"]

    def test_a_spawn_failure_is_swallowed_and_the_rest_continue(self, monkeypatch):
        seen: list[str] = []

        def _picky(cmd, **kwargs):
            if cmd[2] == "api":
                raise OSError("cannot spawn")
            seen.append(cmd[2])
            return _SpawnRecorder(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _picky)
        assert psmux.decorate_sessions_async(["api", "web"]) == ["web"]
        assert set(seen) == {"web"}

    def test_probes_for_code_exactly_once_for_the_batch(self, monkeypatch):
        probes: list[int] = []
        monkeypatch.setattr(psmux, "code_on_path", lambda: (probes.append(1), True)[1])
        psmux.decorate_sessions_async(["api", "web", "docs"])
        assert len(probes) == 1

    def test_an_explicit_hint_skips_the_probe(self, monkeypatch):
        def _boom():
            raise AssertionError("must not probe when the caller already knows")

        monkeypatch.setattr(psmux, "code_on_path", _boom)
        psmux.decorate_sessions_async(["api"], code_hint=False)
        cmds = [cmd for cmd, _ in _SpawnRecorder.spawned]
        assert ["psmux", "-L", "api", "unbind-key", "-n", "F2"] in cmds

    def test_no_binary_or_no_names_is_a_noop(self, monkeypatch):
        assert psmux.decorate_sessions_async([]) == []
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.decorate_sessions_async(["api"]) == []
        assert _SpawnRecorder.spawned == []
        # ...and a no-op must not stamp: nothing was decorated.
        assert not psmux.DECOR_STAMP.exists()
