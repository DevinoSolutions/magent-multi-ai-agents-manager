"""The Session-0 desktop hand-off: the policy, the relay, and (on Windows) the
real Task Scheduler choreography against a FAKE schtasks.

The incident being fixed: `magent attach <host>` runs `magent up` on the host
over ssh, Windows OpenSSH is a service, and so the bring-up -- with 82 psmux
servers and 42 agents -- was born in logon Session 0, invisible to and
unkillable from the desktop it was meant to appear on, while holding every
session name the desktop's own bring-up wanted.

NOTHING here creates a real scheduled task. The Windows tier shadows
``schtasks`` with a recording fake first on PATH, which is the entire reason
``run_on_desktop`` resolves the binary with ``shutil.which`` instead of out of
the system directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from magent.launch import (
    SESSION0_HANDOFF_LINE,
    relay_handoff,
    session0_disposition,
    session0_note,
)
from magent.platform import HandoffResult, Platform
from tests.conftest import FakePlatform


def _policy(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MAGENT_SESSION0_POLICY", value)
    monkeypatch.setattr("magent.env._cached_env", None)


class TestTheDisposition:
    """ONE decision for every caller. A second copy of this policy is how one
    of them creates a Session-0 fleet again."""

    def test_an_interactive_session_always_runs_here(self, monkeypatch):
        # Before the policy is even read: a normal desktop launch must not be
        # able to change behaviour because of a variable somebody set for a
        # headless host.
        _policy(monkeypatch, "refuse")
        plat = FakePlatform(interactive_session=True, supports_handoff=True)

        assert session0_disposition(plat) == "run"

    def test_session_zero_hands_off_by_default(self, monkeypatch):
        _policy(monkeypatch, "handoff")
        plat = FakePlatform(interactive_session=False, supports_handoff=True)

        assert session0_disposition(plat) == "handoff"

    def test_a_platform_that_cannot_hand_off_refuses_instead(self, monkeypatch):
        # "handoff" is a request, not a guarantee. A platform with no mechanism
        # must refuse rather than silently fall through to running in place --
        # running in place is the defect.
        _policy(monkeypatch, "handoff")
        plat = FakePlatform(interactive_session=False, supports_handoff=False)

        assert session0_disposition(plat) == "refuse"

    def test_allow_runs_it_where_it_is(self, monkeypatch):
        # The honest setting for a headless Windows host reached only over ssh:
        # there is no desktop, and Session 0 is where its fleet belongs.
        _policy(monkeypatch, "allow")
        plat = FakePlatform(interactive_session=False, supports_handoff=True)

        assert session0_disposition(plat) == "run"

    def test_refuse_refuses_even_with_a_mechanism(self, monkeypatch):
        _policy(monkeypatch, "refuse")
        plat = FakePlatform(interactive_session=False, supports_handoff=True)

        assert session0_disposition(plat) == "refuse"

    def test_the_note_is_silent_on_an_ordinary_machine(self, monkeypatch):
        plat = FakePlatform(interactive_session=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: plat)

        assert session0_note() is None

    def test_the_note_carries_the_reason_when_blocked(self, monkeypatch):
        # What the "N session(s) failed to come up" printers add, so a casualty
        # list never arrives without its cause.
        _policy(monkeypatch, "refuse")
        plat = FakePlatform(interactive_session=False)
        monkeypatch.setattr("magent.launch.get_platform", lambda: plat)

        note = session0_note()
        assert note is not None
        assert "MAGENT_SESSION0_POLICY=allow" in note


class TestTheRelay:
    """`relay_handoff` is the only thing between the desktop copy's output and
    the user's screen -- including, over ssh, a laptop's screen."""

    def test_it_announces_the_handoff_before_running(self, capsys):
        plat = FakePlatform(supports_handoff=True)

        relay_handoff(plat, ["x"], timeout_s=5)

        assert SESSION0_HANDOFF_LINE in capsys.readouterr().out

    def test_it_passes_the_argv_and_budget_through(self):
        plat = FakePlatform(supports_handoff=True)

        relay_handoff(plat, ["a", "b"], timeout_s=42)

        assert plat.handoffs == [(["a", "b"], 42)]

    def test_the_desktop_exit_code_becomes_ours(self):
        plat = FakePlatform(supports_handoff=True, handoff_result=HandoffResult(rc=3))

        assert relay_handoff(plat, ["x"], timeout_s=5) == 3

    def test_output_is_relayed_on_the_stream_it_came_from(self, capsys):
        # stdout to stdout and stderr to stderr, because the command being
        # handed off IS this command -- and `magent up --json`-style consumers
        # elsewhere depend on that separation holding everywhere.
        plat = FakePlatform(
            supports_handoff=True,
            handoff_result=HandoffResult(rc=0, stdout="up: 3", stderr="warn: x"),
        )

        relay_handoff(plat, ["x"], timeout_s=5)

        captured = capsys.readouterr()
        assert "up: 3" in captured.out
        assert "warn: x" in captured.err
        assert "warn: x" not in captured.out

    def test_a_timeout_is_a_failure_that_says_it_may_still_be_running(self, capsys):
        # Never a fabricated exit code: the desktop copy was not observed to
        # finish, so claiming it failed (or succeeded) would be a guess.
        plat = FakePlatform(
            supports_handoff=True,
            handoff_result=HandoffResult(rc=None, timed_out=True, detail="left at X"),
        )

        rc = relay_handoff(plat, ["x"], timeout_s=7)

        assert rc == 1
        err = capsys.readouterr().err
        assert "timed out after 7s" in err
        assert "may still be running" in err
        assert "launch.log" in err

    def test_a_mechanism_failure_names_the_detail(self, capsys):
        plat = FakePlatform(
            supports_handoff=True,
            handoff_result=HandoffResult(rc=None, detail="schtasks not found"),
        )

        rc = relay_handoff(plat, ["x"], timeout_s=5)

        assert rc == 1
        assert "schtasks not found" in capsys.readouterr().err


class TestThePlatformDefaults:
    """Adding a platform capability = a default on the ABC plus per-OS
    overrides. The defaults are what keep macOS/Linux on today's behaviour."""

    class _Bare(Platform):
        def set_dpi_aware(self) -> None: ...
        def list_monitors(self):
            return []

        def find_window(self, title, mode="exact"):
            return None

        def move_window(self, handle, rect) -> None: ...
        def launch_terminal(self, opts) -> None: ...
        def launch_vscode(self, opts) -> None: ...

    def test_a_platform_is_interactive_until_it_says_otherwise(self):
        # POSIX has no logon-session isolation and tmux over ssh is the normal
        # way to work there, so the whole question must not arise.
        assert self._Bare().logon_session_is_interactive() is True

    def test_nothing_claims_a_handoff_mechanism_by_default(self):
        assert self._Bare().supports_desktop_handoff() is False

    def test_running_on_a_desktop_is_not_implemented_by_default(self):
        with pytest.raises(NotImplementedError):
            self._Bare().run_on_desktop(["x"], timeout_s=1)


# --- The Windows tier: a real create/run/poll/delete against a fake schtasks --

_FAKE_SCHTASKS = """\
import json, pathlib, subprocess, sys, os

here = pathlib.Path(__file__).parent
args = sys.argv[1:]
(here / "calls.jsonl").open("a", encoding="utf-8").write(json.dumps(args) + "\\n")

state = here / "tasks.json"
tasks = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


mode = args[0].lower() if args else ""
name = value("/tn")
if mode == "/create":
    tasks[name] = value("/tr")
    state.write_text(json.dumps(tasks), encoding="utf-8")
elif mode == "/run":
    spec = tasks.get(name)
    if spec is None:
        sys.exit(1)
    if os.environ.get("MDTEST_HANDOFF_NEVER_STARTS") != "1":
        # DEVNULL on all three, or this fake is not faithful: a child that
        # inherits our captured pipes keeps them open, so the CALLER's
        # `subprocess.run(capture_output=True)` blocks until the task finishes
        # and /run stops being the fire-and-forget real schtasks is.
        subprocess.Popen(
            spec,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
elif mode == "/query":
    # Real schtasks prints a table; only the status word is ever read.
    print("TaskName   Next Run Time   Status")
    print(f"{name}   N/A   Ready")
elif mode == "/delete":
    tasks.pop(name, None)
    state.write_text(json.dumps(tasks), encoding="utf-8")
sys.exit(0)
"""

pytestmark_win = pytest.mark.skipif(
    sys.platform != "win32", reason="Task Scheduler hand-off is win32-only"
)


@pytest.fixture
def fake_schtasks(tmp_path, monkeypatch):
    """A recording ``schtasks`` first on PATH that REALLY runs the shim.

    Not a mock of the module's own subprocess calls: the thing worth proving is
    that the shim cmd file, its three result files and the exit-code round trip
    all work, and a stubbed-out runner would prove only that the code calls
    functions. The fake owns the scheduler, not the shim.

    The scratch ROOT is redirected into tmp_path as well. ``run_on_desktop``
    deliberately leaves its directory behind on failure and names it in
    ``detail``, so the tests that drive the failure paths would otherwise
    accumulate residue in the machine's real temp directory on every run.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    helper = bin_dir / "schtasks_helper.py"
    helper.write_text(_FAKE_SCHTASKS, encoding="utf-8")
    (bin_dir / "schtasks.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    scratch = tmp_path / "systemp"
    scratch.mkdir()
    monkeypatch.setattr(
        "magent.platform.windows.tempfile.gettempdir", lambda: str(scratch)
    )
    return bin_dir


def _scratch_root(bin_dir: Path) -> Path:
    """Where the redirected hand-off scratch directories land."""
    return bin_dir.parent / "systemp" / "magent-handoff"


def _calls(bin_dir: Path) -> list[list[str]]:
    log = bin_dir / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


@pytestmark_win
class TestRunOnDesktopOnWindows:
    def _plat(self):
        from magent.platform.windows import WindowsPlatform

        return WindowsPlatform()

    def test_windows_claims_the_mechanism(self):
        assert self._plat().supports_desktop_handoff() is True

    def test_an_ssh_login_is_not_an_interactive_session(self, monkeypatch):
        # On Windows OpenSSH is a SERVICE, so an ssh login is Session 0 by
        # construction. This is a fact about Windows, not a test hook -- there
        # is no configuration in which an ssh login lands on the desktop.
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")

        assert self._plat().logon_session_is_interactive() is False

    def test_this_developer_box_is_interactive(self, monkeypatch):
        for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            monkeypatch.delenv(name, raising=False)

        assert self._plat().logon_session_is_interactive() is True

    def test_the_command_really_runs_and_its_streams_come_back(self, fake_schtasks):
        result = self._plat().run_on_desktop(
            [
                sys.executable,
                "-c",
                (
                    "import sys; print('hello out'); "
                    "print('hello err', file=sys.stderr); sys.exit(7)"
                ),
            ],
            timeout_s=60,
        )

        assert result.rc == 7
        assert result.timed_out is False
        assert "hello out" in result.stdout
        assert "hello err" in result.stderr

    def test_the_child_can_never_hand_off_again(self, fake_schtasks):
        # A hand-off that landed in Session 0 again and handed off in turn
        # would be a recursion whose every level writes a scheduled task.
        result = self._plat().run_on_desktop(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['MAGENT_SESSION0_POLICY'])",
            ],
            timeout_s=60,
        )

        assert result.stdout.strip() == "refuse"

    def test_the_desktop_copy_keeps_our_working_directory(self, fake_schtasks):
        # A scheduled task starts in system32, and find_config walks up from
        # the working directory -- so without this the desktop copy could bring
        # up a different config's projects than the command the user typed.
        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "import os; print(os.getcwd())"], timeout_s=60
        )

        assert Path(result.stdout.strip()) == Path.cwd()

    def test_the_scheduler_argv_is_the_verified_recipe(self, fake_schtasks):
        self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        calls = _calls(fake_schtasks)
        modes = [c[0] for c in calls]
        assert modes == ["/create", "/run", "/delete"]
        create = calls[0]
        task = create[create.index("/tn") + 1]
        assert task.startswith("magent-handoff-")
        # /it is the whole point: "run only when the user is logged on" is what
        # puts the process in the interactive session. /f makes a re-run
        # idempotent; /sc once + /st is the trigger schtasks demands and /run
        # never waits for.
        for flag in ("/it", "/f"):
            assert flag in create
        assert create[create.index("/sc") + 1] == "once"
        assert "/st" in create
        # ...and the delete really names the same task, on every path: a
        # one-shot task left behind fires tonight and re-runs the bring-up.
        assert calls[-1] == ["/delete", "/tn", task, "/f"]

    def test_the_run_spec_stays_under_the_schtasks_limit(self, fake_schtasks):
        self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        create = _calls(fake_schtasks)[0]
        run_spec = create[create.index("/tr") + 1]
        # schtasks truncates /tr silently past 261 characters -- which is why
        # the task runs a shim FILE and not the real command line.
        assert len(run_spec) <= 261
        assert run_spec.endswith('run.cmd"')

    def test_a_slow_command_times_out_without_a_fabricated_code(self, fake_schtasks):
        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "import time; time.sleep(3)"], timeout_s=0.8
        )

        assert result.timed_out is True
        assert result.rc is None
        # The scratch directory is named, because it is the only evidence left.
        assert "scratch left at" in result.detail
        # ...and the task is still deleted: its trigger is past-dated so it can
        # never fire, but a leftover task is clutter and a name the next
        # hand-off cannot reuse.
        assert _calls(fake_schtasks)[-1][0] == "/delete"

    def test_a_task_that_never_starts_is_not_waited_out(
        self, fake_schtasks, monkeypatch
    ):
        # "Nobody is logged on" and "the command is slow" are different answers,
        # and only the first is worth abandoning a 900s budget for.
        monkeypatch.setenv("MDTEST_HANDOFF_NEVER_STARTS", "1")
        monkeypatch.setattr("magent.platform.windows._HANDOFF_START_GRACE_S", 0.2)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=30
        )

        assert result.rc is None
        assert result.timed_out is False
        assert "never started" in result.detail
        assert "logged on" in result.detail

    def test_no_schtasks_is_a_named_failure_not_a_crash(self, tmp_path, monkeypatch):
        empty = tmp_path / "nothing"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

        result = self._plat().run_on_desktop(["whatever"], timeout_s=5)

        assert result == HandoffResult(rc=None, detail="schtasks not found")

    def test_a_successful_handoff_leaves_no_scratch_behind(self, fake_schtasks):
        root = _scratch_root(fake_schtasks)

        self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        assert list(root.iterdir()) == []

    def test_a_failed_handoff_keeps_its_evidence(self, fake_schtasks, monkeypatch):
        # The other half of the same rule: on failure the directory STAYS and
        # is named in `detail`, because the shim and whatever the command
        # managed to write are the only evidence there is.
        monkeypatch.setenv("MDTEST_HANDOFF_NEVER_STARTS", "1")
        monkeypatch.setattr("magent.platform.windows._HANDOFF_START_GRACE_S", 0.2)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=30
        )

        left = list(_scratch_root(fake_schtasks).iterdir())
        assert len(left) == 1
        assert str(left[0]) in result.detail
        assert (left[0] / "run.cmd").is_file()


@pytestmark_win
class TestTheShimItself:
    """The batch file is the only thing standing between an argv and cmd.exe's
    own parsing, so its quoting is worth pinning directly."""

    def test_a_path_with_spaces_survives_the_command_line(self, tmp_path):
        from magent.platform.windows import _handoff_shim

        work = tmp_path / "a b c"
        work.mkdir()
        shim = work / "run.cmd"
        out, err, rc = work / "out.txt", work / "err.txt", work / "rc.txt"
        shim.write_text(
            _handoff_shim(
                [sys.executable, "-c", "print('ok')"], str(work), out, err, rc
            ),
            encoding="utf-8",
        )

        subprocess.run(["cmd", "/c", str(shim)], check=True, timeout=60)

        assert out.read_text(encoding="utf-8").strip() == "ok"
        assert rc.read_text(encoding="utf-8").strip() == "0"
