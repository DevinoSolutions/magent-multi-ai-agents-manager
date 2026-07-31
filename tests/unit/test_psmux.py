"""Unit tests for magent.psmux leaf primitives.

Focused on pane_cwd's subprocess guards: the P1-06 extraction (137c8d5) that
moved the inline session_picker ``cwd()`` closure into psmux.pane_cwd dropped
its timeout=3 / encoding=utf-8 / errors=replace / OSError-swallow guards. These
pins restore and lock them so a psmux that hangs, emits non-utf-8 bytes, or
isn't launchable can never take down a caller (the attention picker fans
pane_cwd across every live session concurrently).
"""

from __future__ import annotations

import subprocess

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


class TestDecorateSession:
    """The status-line hints (` F1 picker  F2 code `), the product-owned
    `bind -n F1 detach-client`, and the product-owned status-left brand (+ the
    length budget it needs). All are `-L <name>`-scoped so they land on that
    session's own server and beat whatever its tmux.conf set at start-up.
    """

    def _run(self, monkeypatch, *, boom: Exception | None = None):
        cmds: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            cmds.append(cmd)
            if boom is not None:
                raise boom
            return _FakeCompleted(returncode=0, stdout="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
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
            " F1 picker  F2 code ",
        ]

    def test_sets_the_status_left_brand(self, monkeypatch):
        cmds = self._run(monkeypatch)
        assert cmds[2] == [
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
        assert cmds[3] == [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left-length",
            "10",
        ]

    def test_status_left_length_fits_the_brand(self):
        # The number is only correct relative to the brand text; pin the
        # relationship, not just the two literals.
        assert int(psmux._STATUS_BRAND_LEN) >= len(" magent ")

    def test_hint_advertises_both_keys(self):
        # The literal is defined once; a rename must not silently drop a key.
        argv = psmux.decoration_argv("api", "psmux")
        assert "F1" in argv[1][-1] and "F2" in argv[1][-1]

    def test_brand_names_the_product(self):
        # Same guard on the other literal: the status-left is branding, so the
        # product name is the part that must survive a restyle.
        argv = psmux.decoration_argv("api", "psmux")
        assert "magent" in argv[2][-1]

    def test_never_raises_when_the_subprocess_fails(self, monkeypatch):
        # A status bar is cosmetic: an unlaunchable/hung psmux is logged and
        # swallowed, never propagated into a bring-up.
        cmds = self._run(monkeypatch, boom=OSError("no psmux"))
        assert len(cmds) == 4  # all attempted; none escaped

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
        monkeypatch.setattr(
            psmux, "decorate_session", lambda n, psmux=None: seen.append(n)
        )
        assert psmux.decorate_sessions(["api", "web"]) == ["api", "web"]
        assert sorted(seen) == ["api", "web"]

    def test_fan_out_without_binary_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        assert psmux.decorate_sessions(["api"]) == []
