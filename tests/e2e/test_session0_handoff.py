"""e2e: a REAL `magent up` child, told it is an ssh login, hands its bring-up
to the desktop instead of creating a fleet nobody can see.

Real processes throughout: a real `python -m magent up`, a real fake ``psmux``
on PATH, a real fake ``schtasks`` that records its argv and really executes the
shim the hand-off writes. The only fiction is which binaries PATH resolves.

Why ``SSH_CONNECTION`` is the trigger rather than a monkeypatched session id: a
child process on a developer's box genuinely runs in logon session 1, so the id
alone could never exercise this path from a real process. It is not a test
hook either -- Windows OpenSSH is a SERVICE, so every process an ssh login
spawns is in Session 0 by construction, which is exactly what
``WindowsPlatform.logon_session_is_interactive`` documents.

Nothing here creates a real scheduled task, binds a port, or touches the real
``~/.magent``: HOME is redirected in full and both binaries are fakes first on
PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path  # noqa: TC003  # reason: runtime use in fixture bodies

import pytest

pytestmark = pytest.mark.e2e

# Whole-test wall clock. Two real python child processes plus a shim round trip;
# anything past this is a hang worth failing on, not a slow machine.
_BUDGET_S = 120.0

_FAKE_SCHTASKS = """\
import json, os, pathlib, subprocess, sys

here = pathlib.Path(__file__).parent
args = sys.argv[1:]
(here / "calls.jsonl").open("a", encoding="utf-8").write(json.dumps(args) + "\\n")

state = here / "tasks.json"
tasks = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
name = args[args.index("/tn") + 1] if "/tn" in args else None
mode = args[0].lower() if args else ""
if mode == "/create":
    tasks[name] = args[args.index("/tr") + 1]
    state.write_text(json.dumps(tasks), encoding="utf-8")
elif mode == "/run":
    spec = tasks.get(name)
    if spec is None:
        sys.exit(1)
    # Task Scheduler builds the task's environment from the USER PROFILE, not
    # from whoever called schtasks -- so the desktop copy does not inherit the
    # ssh login's variables. Modelling that is what makes this fake honest:
    # leave them in and the handed-off magent would see itself as an ssh login
    # too, which is the one thing the real mechanism guarantees it is not.
    child = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
    # DEVNULL on all three: a child that inherited our captured pipes would
    # keep them open, and the caller's capture_output=True would then block
    # until the task finished -- which real schtasks never does.
    subprocess.Popen(
        spec,
        shell=True,
        env=child,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
elif mode == "/query":
    print("TaskName   Next Run Time   Status")
    print(f"{name}   N/A   Ready")
elif mode == "/delete":
    tasks.pop(name, None)
    state.write_text(json.dumps(tasks), encoding="utf-8")
sys.exit(0)
"""


def _fake_psmux(bin_dir: Path) -> None:
    """A multiplexer that answers everything with success and creates nothing.

    `up` only needs psmux to EXIST and to be probeable here; what is under test
    is whether the bring-up happens on this side of the hand-off at all.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (bin_dir / "psmux.bat").write_text(
            "@echo off\r\nexit /b 0\r\n", encoding="utf-8"
        )
    else:
        shim = bin_dir / "psmux"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)


def _fake_schtasks(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    helper = bin_dir / "schtasks_helper.py"
    helper.write_text(_FAKE_SCHTASKS, encoding="utf-8")
    (bin_dir / "schtasks.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )


def _child_env(home: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    # The three standing test-isolation opt-outs: no system-wide keyboard hook,
    # no real detached upload server, no priority sweep over the developer's
    # real psmux fleet (which no HOME redirect can contain).
    env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
    env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
    env["MAGENT_PSMUX_BOOST"] = "0"
    home.mkdir(parents=True, exist_ok=True)
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["HOME"] = home_s
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or os.sep
    # Our fakes must win every PATH lookup the child makes.
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env.update(extra)
    return env


def _config(tmp_path: Path) -> Path:
    (tmp_path / "api").mkdir(exist_ok=True)
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": [{"path": str(tmp_path / "api"), "tool": "claude"}],
                "settings": {"uploadServer": False},
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _run_up(cfg: Path, env: dict[str, str], *args: str, timeout: float):
    return subprocess.run(
        [sys.executable, "-m", "magent", "--config", str(cfg), "up", *args],
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
        timeout=timeout,
    )


def _calls(bin_dir: Path) -> list[list[str]]:
    log = bin_dir / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(
    sys.platform != "win32", reason="the Task Scheduler hand-off is win32-only"
)
class TestAWindowsSshLoginNeverCreatesSessionsInPlace:
    """What a real `magent up` child does when it is told it is an ssh login.

    DELIBERATE GAP: there is no real-process test of the hand-off SUCCEEDING.
    `run_on_desktop` resolves schtasks out of the system directory precisely so
    an ssh login's PATH cannot choose what runs as the logged-on user, which
    means a child process cannot be pointed at a fake -- and the alternatives
    are a test-only environment variable (forbidden) or writing a REAL
    scheduled task on the machine running the suite (worse). The full
    create/run/poll/delete choreography is proven in
    ``tests/unit/test_desktop_handoff.py`` against the ``_schtasks_exe`` seam,
    with real processes on both ends of the launcher. What lives here is the
    half a child process CAN prove: the detection, and that nothing is ever
    created in place.
    """

    def test_refuse_creates_nothing_and_says_why(self, tmp_path):
        started = time.monotonic()
        bin_dir = tmp_path / "bin"
        _fake_psmux(bin_dir)
        _fake_schtasks(bin_dir)
        env = _child_env(
            tmp_path / "home",
            bin_dir,
            SSH_CONNECTION="1.2.3.4 1 5.6.7.8 22",
            MAGENT_SESSION0_POLICY="refuse",
        )

        result = _run_up(_config(tmp_path), env, timeout=_BUDGET_S)

        assert result.returncode == 1
        assert "refusing to start psmux sessions" in result.stderr
        # It never reached the bring-up banner, so no session was attempted.
        assert "Bring up sessions" not in result.stdout
        # Nothing was scheduled either: a refusal must not also write a task.
        assert _calls(bin_dir) == []
        assert time.monotonic() - started < _BUDGET_S

    def test_allow_is_the_escape_hatch_for_a_headless_host(self, tmp_path):
        # The same child, same ssh environment, one policy value apart: a
        # genuinely headless Windows host reached only over ssh has no desktop
        # to hand off to, and Session 0 is where its fleet belongs.
        bin_dir = tmp_path / "bin"
        _fake_psmux(bin_dir)
        _fake_schtasks(bin_dir)
        env = _child_env(
            tmp_path / "home",
            bin_dir,
            SSH_CONNECTION="1.2.3.4 1 5.6.7.8 22",
            MAGENT_SESSION0_POLICY="allow",
        )

        result = _run_up(_config(tmp_path), env, timeout=_BUDGET_S)

        assert result.returncode == 0, result.stderr
        assert "Bring up sessions" in result.stdout
        assert "refusing to start psmux sessions" not in result.stderr
        assert _calls(bin_dir) == []


class TestAPosixSshLoginIsOrdinaryWork:
    """POSIX has no logon-session isolation and tmux over ssh is how people
    work there, so an ssh login must change NOTHING -- no hand-off, no refusal,
    and no dependence on the hand-off policy at all."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the POSIX side of the same question"
    )
    def test_an_ssh_login_brings_up_normally(self, tmp_path):
        bin_dir = tmp_path / "bin"
        _fake_psmux(bin_dir)
        env = _child_env(
            tmp_path / "home",
            bin_dir,
            SSH_CONNECTION="1.2.3.4 1 5.6.7.8 22",
            # The strictest policy there is: it must still not bite, because
            # the platform reports an interactive session before the policy is
            # ever read.
            MAGENT_SESSION0_POLICY="refuse",
        )

        result = _run_up(_config(tmp_path), env, timeout=_BUDGET_S)

        assert result.returncode == 0, result.stderr
        assert "re-running on the desktop" not in result.stdout
        assert "refusing to start psmux sessions" not in result.stdout
        assert "Bring up sessions" in result.stdout
