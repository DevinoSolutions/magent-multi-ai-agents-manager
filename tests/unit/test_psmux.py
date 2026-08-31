"""Unit tests for magent.psmux leaf primitives.

Focused on pane_cwd's subprocess guards: the P1-06 extraction (137c8d5) that
moved the inline session_picker ``cwd()`` closure into psmux.pane_cwd dropped
its timeout=3 / encoding=utf-8 / errors=replace / OSError-swallow guards. These
pins restore and lock them so a psmux that hangs, emits non-utf-8 bytes, or
isn't launchable can never take down a caller (the attention picker fans
pane_cwd across every live session concurrently).
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import re
import subprocess
import sys
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


class TestEligibleProjectsFreshStart:
    """`cmd` is the command every psmux consumer runs -- bring_up creates
    sessions with it, revive_sessions injects it into a fallen-back pane, and
    `up --json` ships it to the attach client for no-mux windows. A project
    directory with no stored conversation must not get `claude --continue`
    there: claude exits with "no conversation found", the pane is left at a
    dead shell, and revive re-runs the same failing command forever."""

    def _cmd(self, monkeypatch, tmp_path, *, has_session):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session",
            lambda project_dir, home_override=None: has_session,
        )
        cfg = _cfg(
            [ProjectConfig(path=str(tmp_path), tool="claude", title="api")],
            tools={"claude": "claude --continue"},
        )
        return psmux.eligible_projects(cfg)[0]["cmd"]

    def test_a_directory_with_no_conversation_starts_fresh(self, monkeypatch, tmp_path):
        assert self._cmd(monkeypatch, tmp_path, has_session=False) == "claude"

    def test_a_directory_with_a_conversation_still_continues_it(
        self, monkeypatch, tmp_path
    ):
        assert self._cmd(monkeypatch, tmp_path, has_session=True) == "claude --continue"

    def test_an_unresolvable_folder_leaves_the_command_alone(self, monkeypatch):
        # Nothing on this machine to probe -- and the entry is reported down
        # with "folder not found" anyway. Guessing "new" here would be a
        # verdict taken with no evidence.
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session",
            lambda project_dir, home_override=None: pytest.fail(
                "probed a folder that does not resolve"
            ),
        )
        cfg = _cfg(
            [ProjectConfig(path="/nope/api", tool="claude")],
            tools={"claude": "claude --continue"},
        )
        assert psmux.eligible_projects(cfg)[0]["cmd"] == "claude --continue"

    def test_a_tool_with_no_configured_command_stays_empty(self, tmp_path):
        # "" is how psmux_status/bring_up spell "no agent command" -- the
        # fresh-start probe must not turn it into something runnable.
        cfg = _cfg([ProjectConfig(path=str(tmp_path), tool="ghost")], tools={})
        assert psmux.eligible_projects(cfg)[0]["cmd"] == ""


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


def _markers_in(env: object) -> list[str]:
    """The nesting markers still present in a child's environment.

    Deliberately NOT a blanket ``PSMUX*``/``TMUX*`` sweep: ``TMUX_TMPDIR`` is
    the socket directory and MUST survive (see
    test_env_schema.py::TestPsmuxChildEnv::test_the_socket_dir_survives), so a
    prefix assertion here would demand the very bug that broke CI.
    """
    assert isinstance(env, dict)
    return [
        k
        for k in env
        if not k.upper().endswith("_TMPDIR")
        and (k.upper() in ("TMUX", "TMUX_PANE") or k.upper().startswith("PSMUX"))
    ]


class TestControlCommandsInheritTheEnvironment:
    """Only SESSION CREATION gets the cleaned environment; control and probe
    commands spawn with plain inheritance.

    Scope measured against real psmux 3.3.6 from inside a live pane: with the
    markers present and with them stripped, ``has-session -t``,
    ``display-message -t`` and ``capture-pane -t`` return the same exit code
    and the same bytes -- the nested-session guard only ever fires for
    ``new-session``. Cleaning the environment here would buy nothing while
    putting a rebuilt environment block under every psmux round-trip magent
    makes, and the launch path makes hundreds. The one thing an inherited
    ``$TMUX`` could still do -- let a target-less command answer for the
    CALLER's own pane -- is closed explicitly by the ``-t <session>`` every
    command in this module now passes.
    """

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
    def test_no_env_is_passed_at_all(self, monkeypatch, call):
        # `env=None` is "inherit", which is what every one of these wants.
        assert self._env_of(monkeypatch, call) == [None]

    def test_the_accessor_still_strips_for_the_creating_caller(self):
        # `child_env` stays: platform/windows.py's `new-session` is its one
        # caller, and that IS the command the guard fires for.
        assert _markers_in(psmux.child_env()) == []

    def test_the_decoration_pass_inherits_too(self, monkeypatch):
        monkeypatch.setattr(psmux, "code_on_path", lambda: True)
        assert set(
            self._env_of(
                monkeypatch, lambda: psmux.decorate_session("api", psmux="psmux")
            )
        ) == {None}

    def test_the_pane_command_fan_out_inherits_too(self, monkeypatch):
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
        assert seen == [None]

    def test_the_accessor_preserves_the_rest_of_the_environment(self, monkeypatch):
        monkeypatch.setenv("MDTEST_KEEP", "yes")
        assert psmux.child_env()["MDTEST_KEEP"] == "yes"


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
        # The tmp project dir has no stored claude conversation, so
        # `eligible_projects` already dropped --continue (build_start_command);
        # what matters here is that the respawn replays THAT command verbatim
        # rather than rebuilding a different one.
        assert retried.command == "claude"
        assert retried.command == fp.launched_psmux[0].command
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
    def test_the_argv_is_the_ten_decorations(self, code_hint):
        # The list is what every call site fans out, so its shape is contract:
        # an eleventh command (or a dropped one) has to be a deliberate edit
        # here. Gating F2 changes the hint TEXT and the F2 command's form,
        # never the `set`s -- dropping one of those would leave a personal
        # tmux.conf's value in place. Commands 7-10 own the WINDOW NAME and
        # its rendering: psmux's automatic-rename showed the pane's command
        # ("0:claude.exe.old" after a Claude self-update), so every decoration
        # pass pins the window name back to the project, turns the auto-rename
        # off, and renders the bar entry as the name alone -- no `0:` index
        # (one window per session makes it noise), verified live on 3.3.8.
        argv = psmux.decoration_argv("api", "psmux", code_hint)
        assert len(argv) == 10
        assert argv[0][3:] == ["bind", "-n", "F1", "detach-client"]
        assert [cmd[5] for cmd in argv[1:5]] == [
            "status-right",
            "status-right-length",
            "status-left",
            "status-left-length",
        ]
        assert argv[5][3] in {"bind", "unbind-key"}
        assert argv[6][3:] == ["rename-window", "-t", "api", "api"]
        assert argv[7][3:] == ["set", "-g", "automatic-rename", "off"]
        assert argv[8][3:] == ["set", "-g", "window-status-format", "#W"]
        assert argv[9][3:] == ["set", "-g", "window-status-current-format", "#W"]

    def test_a_long_name_renames_to_the_truncated_display_form(self):
        # The rename argument is the DISPLAY name; the `-t` target stays the
        # session name so a re-decoration of an already-truncated window still
        # resolves (a session target names its current window, whatever it is
        # called).
        name = "magent-multi-ai-agents-manager"
        argv = psmux.decoration_argv(name, "psmux", False)
        rename = argv[6]
        assert rename[3:6] == ["rename-window", "-t", name]
        assert rename[6] == "magent-multi-..."

    def test_window_display_name_truncates_only_past_the_budget(self):
        # <= 16 columns: untouched, including exactly 16. Past it: first 13 +
        # "..." == 16 columns, pure ASCII (the bar's cell-arithmetic law).
        assert psmux.window_display_name("api") == "api"
        assert psmux.window_display_name("a" * 16) == "a" * 16
        long_form = psmux.window_display_name("a" * 17)
        assert long_form == "a" * 13 + "..."
        assert len(long_form) == 16
        assert long_form.isascii()

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
        assert len(cmds) == 10  # all attempted; none escaped

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

    def test_a_stamp_a_hair_in_the_future_is_still_fresh(self):
        # Not a hypothetical: this test's sibling above used to flake on
        # windows-latest/py3.10+3.11 with `assert ['api'] == []` because the
        # throttle read its own fresh stamp as future-dated. The stamp's age is
        # a difference between two readings of the same wall clock (`time.time()`
        # vs a filesystem mtime), and before CPython 3.13 -- where Windows'
        # `time.time()` was GetSystemTimeAsFileTime, 15.625ms granular -- both
        # reads land in the SAME tick and the answer is decided by float
        # rounding: `os.stat` builds st_mtime as `sec + 1e-9*nsec`, `time.time()`
        # divides an integer nanosecond count, and the two disagree by one ULP.
        # Measured on this box under 3.10: 10.2% of 3000 create-then-read cycles
        # came out at -2.384185791015625e-07s. Under 3.13 (precise clock): 0%.
        psmux.decorate_sessions_async(["api"])
        fired = len(_SpawnRecorder.spawned)
        ahead = time.time() + psmux._STAMP_FUTURE_SLOP_S / 2
        os.utime(psmux.DECOR_STAMP, (ahead, ahead))
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
        # "Ahead" here has to stay well past `_STAMP_FUTURE_SLOP_S` -- inside
        # that slop a future date is measurement noise, not a bad clock.
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


class _LiveProbe:
    """Stand-in for one fanned-out `has-session` probe, logging when it is
    waited on so the spawn-then-wait ordering can be pinned. A ``None``
    returncode means "this probe never answers"."""

    def __init__(self, name: str, returncode: int | None, events: list[str]) -> None:
        self._name = name
        self._returncode = returncode
        self._events = events
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self._events.append(f"wait:{self._name}")
        if self._returncode is None:
            raise subprocess.TimeoutExpired(cmd="psmux", timeout=timeout or 0)
        return self._returncode

    def kill(self) -> None:
        self.killed = True


def _fan_out(monkeypatch, results: dict[str, list[bool]]) -> list[str]:
    """Patch Popen so each `has-session` probe pops the next result for its
    session. Returns the spawn/wait event log."""
    events: list[str] = []

    def _fake_popen(cmd, **kwargs):
        name = cmd[2]
        events.append(f"spawn:{name}")
        return _LiveProbe(name, 0 if results[name].pop(0) else 1, events)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    return events


class TestLiveSessions:
    """The ONE liveness enumeration. These pins moved here from
    session_picker, which used to own the product's only retrying probe:
    `psmux_status` (and therefore `status` and `down`) ran the same fan-out
    with NO retry, so the picker and the shutdown could disagree about the same
    session at the same moment -- and the shutdown's direction of error was to
    silently skip a live one."""

    def test_retries_misses_once(self, monkeypatch):
        # First probe flaps b and c to False; the retry recovers b.
        _fan_out(monkeypatch, {"a": [True], "b": [False, True], "c": [False, False]})
        assert psmux.live_sessions(["a", "b", "c"], "psmux") == ["a", "b"]

    def test_config_order_preserved(self, monkeypatch):
        _fan_out(monkeypatch, {"z": [True], "m": [True], "a": [True]})
        assert psmux.live_sessions(["z", "m", "a"], "psmux") == ["z", "m", "a"]

    def test_no_retry_when_all_alive(self, monkeypatch):
        events = _fan_out(monkeypatch, {"a": [True], "b": [True]})
        psmux.live_sessions(["a", "b"], "psmux")
        assert [e for e in events if e.startswith("spawn:")] == ["spawn:a", "spawn:b"]

    def test_retries_zero_takes_the_first_answer(self, monkeypatch):
        # The bring-up creation verify's mode: it owns its own respawn cycle,
        # so a probe retry folded in here would only delay the respawn.
        _fan_out(monkeypatch, {"a": [False, True]})
        assert psmux.live_sessions(["a"], "psmux", retries=0) == []

    def test_a_timed_out_probe_is_not_live_and_is_killed(self, monkeypatch):
        procs: list[_LiveProbe] = []

        def _fake_popen(cmd, **kwargs):
            proc = _LiveProbe(cmd[2], None, [])
            procs.append(proc)
            return proc

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        assert psmux.live_sessions(["a"], "psmux", timeout=0.1, retries=0) == []
        assert [p.killed for p in procs] == [True]

    def test_every_probe_is_spawned_before_any_is_waited_on(self, monkeypatch):
        # The point of the fan-out: n concurrent psmux round-trips, not n
        # sequential ones (nor ceil(n/16) thread-pool batches).
        events = _fan_out(monkeypatch, {"a": [True], "b": [True], "c": [True]})
        psmux.live_sessions(["a", "b", "c"], "psmux")
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
            return _LiveProbe(cmd[2], 0, [])

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        psmux.live_sessions(["a"], "psmux")
        # `-t a` is load-bearing, not decoration: a BARE has-session exits 0
        # against a socket with no server at all (psmux 3.3.6 answers from its
        # internal __warm__ spare), so this sweep listed every configured
        # session as live and the picker offered dead ones for attaching.
        assert argvs == [["psmux", "-L", "a", "has-session", "-t", "a"]]

    def test_probes_inherit_the_environment(self, monkeypatch):
        # A PROBE is not a session-creating command, and psmux's nested guard
        # only fires for `new-session` (measured against the real binary), so
        # this stays a plain inherited spawn; `-t` above is what makes the
        # answer truthful.
        envs: list[object] = []

        def _fake_popen(cmd, **kwargs):
            envs.append(kwargs.get("env"))
            return _LiveProbe(cmd[2], 0, [])

        monkeypatch.setenv("PSMUX_SESSION", "api")
        monkeypatch.setenv("TMUX", "/tmp/sock,1,0")
        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        psmux.live_sessions(["a"], "psmux")
        assert envs == [None]

    def test_an_unspawnable_probe_is_not_live_rather_than_raising(self, monkeypatch):
        def _boom(cmd, **kwargs):
            raise OSError("no fork today")

        monkeypatch.setattr(subprocess, "Popen", _boom)
        assert psmux.live_sessions(["a"], "psmux") == []

    def test_no_binary_or_no_names_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: pytest.fail("must not probe")
        )
        assert psmux.live_sessions([], "psmux") == []
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.live_sessions(["a"]) == []


class TestPsmuxStatusLiveness:
    """`psmux_status` reads liveness through `live_sessions`, retry included --
    it used to run a private single-shot fan-out, which is how `status` could
    call a session stopped while the picker was attached to it."""

    def _cfgdir(self, tmp_path, names):
        projects = []
        for name in names:
            (tmp_path / name).mkdir()
            projects.append(ProjectConfig(path=str(tmp_path / name), tool="claude"))
        return _cfg(projects, tools={"claude": "claude"})

    def test_a_flapping_probe_is_retried_not_reported_down(self, monkeypatch, tmp_path):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        _fan_out(monkeypatch, {"api": [True], "web": [False, True]})
        up, down, _all = psmux.psmux_status(self._cfgdir(tmp_path, ["api", "web"]))
        assert [u["name"] for u in up] == ["api", "web"]
        assert down == []

    def test_a_genuinely_dead_session_survives_the_retry(self, monkeypatch, tmp_path):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        _fan_out(monkeypatch, {"api": [True], "web": [False, False]})
        up, down, _all = psmux.psmux_status(self._cfgdir(tmp_path, ["api", "web"]))
        assert [u["name"] for u in up] == ["api"]
        assert [d["name"] for d in down] == ["web"]


class _StopHarness:
    """Drives `stop_sessions` against a fake psmux: ``alive`` is the world, and
    ``stubborn`` names sessions that ignore their first kill (psmux 3.3.6 still
    exits 0 for those, which is why the answer comes from a re-probe)."""

    def __init__(self, monkeypatch, *, alive, stubborn=()):
        self.alive = set(alive)
        self.stubborn = set(stubborn)
        self.kills: list[str] = []
        self.probes: list[list[str]] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux.time, "sleep", lambda _s: None)
        monkeypatch.setattr(psmux, "kill_server", self._kill)
        monkeypatch.setattr(psmux, "live_sessions", self._live)

    def _kill(self, name, psmux=None):
        self.kills.append(name)
        if name in self.stubborn:
            self.stubborn.discard(name)
        else:
            self.alive.discard(name)
        return True

    def _live(self, names, psmux=None, **kwargs):
        self.probes.append(list(names))
        return [n for n in names if n in self.alive]


class TestStopSessions:
    """`down` reports the world, not the loop it ran.

    Live repro: `magent down --all` printed "Stopped 46 session(s)" while 11 of
    them were still alive and attachable, because `kill_servers` discarded
    every `kill_server` return value and answered with the full list of names
    it had tried."""

    def test_reports_only_sessions_it_proved_stopped(self, monkeypatch):
        _StopHarness(monkeypatch, alive={"api", "web"})
        assert psmux.stop_sessions(["api", "web"]) == (["api", "web"], [])

    def test_a_survivor_is_named_not_counted_as_stopped(self, monkeypatch):
        harness = _StopHarness(monkeypatch, alive={"api", "web"})
        monkeypatch.setattr(
            psmux,
            "kill_server",
            lambda name, psmux=None: (
                harness.kills.append(name),
                name != "web" and harness.alive.discard(name),
                True,
            )[-1],
        )
        assert psmux.stop_sessions(["api", "web"]) == (["api"], ["web"])

    def test_a_stubborn_session_gets_a_second_kill(self, monkeypatch):
        harness = _StopHarness(monkeypatch, alive={"api", "web"}, stubborn={"web"})
        assert psmux.stop_sessions(["api", "web"]) == (["api", "web"], [])
        assert harness.kills == ["api", "web", "web"]

    def test_a_session_that_was_never_running_is_not_claimed_as_stopped(
        self, monkeypatch
    ):
        _StopHarness(monkeypatch, alive={"api"})
        assert psmux.stop_sessions(["api", "ghost"]) == (["api"], [])

    def test_every_name_is_killed_even_ones_the_probe_called_dead(self, monkeypatch):
        # THE fix for the reported bug: a session the liveness probe missed
        # must still be killed. kill-server against a socket with no server is
        # a harmless no-op, so over-targeting costs nothing -- while skipping
        # whatever the probe missed leaves it running forever.
        harness = _StopHarness(monkeypatch, alive=set())
        psmux.stop_sessions(["api", "web"])
        assert sorted(harness.kills) == ["api", "web"]

    def test_no_binary_or_no_names_claims_nothing(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.stop_sessions(["api"]) == ([], [])
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        assert psmux.stop_sessions([]) == ([], [])

    def test_a_survivor_is_logged_as_an_error(self, monkeypatch, caplog):
        _StopHarness(monkeypatch, alive={"web"})
        monkeypatch.setattr(psmux, "kill_server", lambda name, psmux=None: True)
        with caplog.at_level(logging.ERROR, logger="magent.launch"):
            psmux.stop_sessions(["web"])
        assert "still running after two kill attempts" in caplog.text


class TestKillPrimitives:
    def test_kill_server_never_raises_out_of_a_hung_psmux(self, monkeypatch):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="psmux", timeout=1)

        monkeypatch.setattr(subprocess, "run", _boom)
        assert psmux.kill_server("api", psmux="psmux") is False

    def test_kill_server_is_bounded(self, monkeypatch):
        seen: list[object] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _FakeCompleted(0, "")

        monkeypatch.setattr(subprocess, "run", _run)
        psmux.kill_server("api", psmux="psmux")
        # A wedged psmux server answers nothing at all; one stuck socket must
        # not hold a 46-session shutdown hostage.
        assert seen == [psmux._KILL_TIMEOUT_S]

    def test_kill_servers_attempts_every_name(self, monkeypatch):
        killed: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "kill_server", lambda n, psmux=None: (killed.append(n), True)[1]
        )
        assert psmux.kill_servers(["a", "b"]) == ["a", "b"]
        assert sorted(killed) == ["a", "b"]


class TestSendKeysIsBounded:
    """``send-keys`` was the one psmux call in this module with NO timeout, and
    the one an HTTP request handler ran inline. An Alt+V upload was measured
    answering 74 s after the press because of it -- by which time the listener
    had given up at 20 s and told the user the upload had failed."""

    def _timeouts(self, monkeypatch) -> list[object]:
        seen: list[object] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _FakeCompleted(0, "")

        monkeypatch.setattr(subprocess, "run", _run)
        return seen

    def test_it_asks_for_a_timeout_by_default(self, monkeypatch):
        seen = self._timeouts(monkeypatch)
        psmux.send_keys("api", "hello", psmux="psmux")
        assert seen == [psmux.SEND_KEYS_TIMEOUT_S]
        assert psmux.SEND_KEYS_TIMEOUT_S > 0

    def test_a_caller_with_its_own_budget_wins(self, monkeypatch):
        seen = self._timeouts(monkeypatch)
        psmux.send_keys("api", "hello", psmux="psmux", timeout=5.0)
        assert seen == [5.0]

    def test_a_hung_psmux_is_a_false_not_an_exception(self, monkeypatch, caplog):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="psmux", timeout=1)

        monkeypatch.setattr(subprocess, "run", _boom)
        with caplog.at_level(logging.WARNING, logger="magent.launch"):
            assert psmux.send_keys("api", "hello", psmux="psmux") is False
        # Silence is how a paste vanishes without a trace; the give-up names
        # the project and the wait it cost.
        assert "send-keys" in caplog.text and "api" in caplog.text

    def test_an_unlaunchable_psmux_is_a_false_too(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no such binary")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert psmux.send_keys("api", "hello", psmux="psmux") is False


class TestProbeControlPlane:
    """The doctor-only responsiveness probe. Its whole job is to come back:
    the failure it looks for is a psmux that answers NOTHING, machine-wide,
    for as long as the box stays up (2026-08-18/19), so an unbounded or
    raising probe would reproduce the outage inside the tool meant to
    diagnose it."""

    def _run(self, monkeypatch, run):
        monkeypatch.setattr(subprocess, "run", run)
        return psmux.probe_control_plane(psmux="psmux")

    def test_an_answer_is_responsive_whatever_the_exit_code(self, monkeypatch):
        # "no server running on this socket" is rc != 0 and is a perfectly
        # healthy ANSWER -- the probe measures replies, not sessions.
        probe = self._run(monkeypatch, lambda cmd, **kw: _FakeCompleted(returncode=1))
        assert (probe.responsive, probe.timed_out) == (True, False)
        assert probe.elapsed_s >= 0.0

    def test_a_timeout_is_the_finding_not_an_exception(self, monkeypatch):
        def _hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        probe = self._run(monkeypatch, _hang)
        assert (probe.responsive, probe.timed_out) == (False, True)

    def test_an_unlaunchable_binary_is_not_a_wedge(self, monkeypatch):
        # A psmux that will not start is a broken install, not a frozen fleet,
        # and the two repair hints could not be more different.
        def _boom(cmd, **kwargs):
            raise OSError("no such binary")

        probe = self._run(monkeypatch, _boom)
        assert (probe.responsive, probe.timed_out) == (False, False)

    def test_no_binary_at_all_answers_without_probing(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: pytest.fail("probed with no binary")
        )
        probe = psmux.probe_control_plane()
        assert (probe.responsive, probe.timed_out) == (False, False)

    def test_the_bound_is_handed_to_subprocess_and_is_short(self, monkeypatch):
        seen: list[object] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs.get("timeout"))
            return _FakeCompleted(returncode=0)

        self._run(monkeypatch, _run)
        assert seen == [psmux.CONTROL_PROBE_TIMEOUT_S]
        assert 0 < psmux.CONTROL_PROBE_TIMEOUT_S <= 10

    def test_it_never_captures_output(self, monkeypatch):
        """Measured, not stylistic: with ``capture_output=True`` the timeout is
        not a bound at all on Windows -- ``subprocess.run`` kills the direct
        child and then waits on the pipes, which a grandchild of a wedged psmux
        still holds. The first build of this probe answered a 5 s timeout in
        90 s for exactly that reason."""
        seen: list[dict] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs)
            return _FakeCompleted(returncode=0)

        self._run(monkeypatch, _run)
        assert seen[0].get("capture_output") is None
        assert seen[0]["stdout"] is subprocess.DEVNULL
        assert seen[0]["stderr"] is subprocess.DEVNULL

    def test_it_probes_its_own_socket_and_enumerates_nothing(self, monkeypatch):
        """Not a fourth liveness sweep: no configured session is named, and
        the command cannot start a server on a project's socket."""
        seen: list[list[str]] = []

        def _run(cmd, **kwargs):
            seen.append(list(cmd))
            return _FakeCompleted(returncode=0)

        self._run(monkeypatch, _run)
        assert seen == [["psmux", "-L", psmux.CONTROL_PROBE_SOCKET, "list-sessions"]]
        assert "has-session" not in seen[0]
        assert "new-session" not in seen[0]


class TestEveryOneShotSpawnHidesItsConsole:
    """Every psmux control/probe spawn must carry ``creationflags=_SPAWN_FLAGS``.

    Why this is a contract and not hygiene: the fleet's supervised processes
    (``magent serve``, ``attention -d``, the hotkey listener) run with NO
    console (``launch.spawn_detached`` uses DETACHED_PROCESS|CREATE_NO_WINDOW),
    and on Windows a console-subsystem child of a console-less parent is given
    a brand-new console -- which Windows 11's default-terminal setting
    materializes as a real, empty Windows Terminal window. Observed live
    2026-08-31: one Alt+V press (three narration flashes + the paste
    ``send-keys`` + a discovery fan-out probing every configured session)
    opened dozens of empty terminals at once and froze the desktop. A single
    forgotten call site brings the storm back, so the AST is walked instead of
    trusting review to catch the fourteenth spawn.
    """

    def test_every_subprocess_call_in_psmux_carries_the_flags(self):
        offenders: list[int] = []
        tree = ast.parse(inspect.getsource(psmux))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr
                in ("run", "Popen", "call", "check_call", "check_output")
                and "creationflags" not in [k.arg for k in node.keywords]
            ):
                offenders.append(node.lineno)
        assert not offenders, (
            f"psmux.py spawns a subprocess without creationflags at line(s) "
            f"{offenders} -- from a console-less serve/daemon each such spawn "
            f"opens an empty terminal window on Windows"
        )

    def test_the_flags_hide_the_console_exactly_on_windows(self):
        """CREATE_NO_WINDOW on win32 (hand-defined -- the stdlib attribute only
        exists there), and exactly 0 elsewhere so POSIX Popen accepts it."""
        if sys.platform == "win32":
            assert psmux._SPAWN_FLAGS == subprocess.CREATE_NO_WINDOW
        else:
            assert psmux._SPAWN_FLAGS == 0
