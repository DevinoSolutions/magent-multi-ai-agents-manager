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


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess with just the fields
    pane_cwd reads."""

    def __init__(self, returncode: int = 0, stdout: str | None = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class TestPaneCwd:
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
