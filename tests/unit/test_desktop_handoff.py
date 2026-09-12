"""The Session-0 desktop hand-off: the policy, the relay, and (on Windows) the
real Task Scheduler choreography against a FAKE schtasks.

The incident being fixed: `magent attach <host>` runs `magent up` on the host
over ssh, Windows OpenSSH is a service, and so the bring-up -- with 82 psmux
servers and 42 agents -- was born in logon Session 0, invisible to and
unkillable from the desktop it was meant to appear on, while holding every
session name the desktop's own bring-up wanted.

NOTHING here creates a real scheduled task. The Windows tier replaces the
``_schtasks_exe`` seam with a recording fake -- NOT by shadowing PATH, because
the real resolver reads the system directory first (an ssh login's PATH must
not get to choose what runs as the logged-on user), so a PATH plant would miss
and write a real task. Everything downstream of the scheduler is real: the
generated PowerShell, the four result files, and the exit-code round trip.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from magent.launch import (
    SESSION0_HANDOFF_LINE,
    relay_handoff,
    session0_disposition,
    session0_note,
    session0_refusal,
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


class TestTheRefusalNamesItsCause:
    """Two different situations wear the same `refuse` disposition, and telling
    them apart is the difference between actionable advice and advice the user
    cannot take."""

    def test_a_policy_refusal_says_run_it_on_the_desktop(self, monkeypatch):
        _policy(monkeypatch, "refuse")
        plat = FakePlatform(interactive_session=False, supports_handoff=True)

        assert "Run 'magent up' on the host's own desktop" in session0_refusal(plat)

    def test_no_desktop_says_nobody_is_logged_on(self, monkeypatch):
        # The policy DID ask for a hand-off; there is simply nowhere to hand
        # off to. "Run it on the desktop" would be advice with no desktop.
        _policy(monkeypatch, "handoff")
        plat = FakePlatform(interactive_session=False, supports_handoff=False)

        reason = session0_refusal(plat)
        assert "no user is logged on" in reason
        assert "Run 'magent up' on the host's own desktop" not in reason

    def test_the_serve_wording_is_carried_through(self, monkeypatch):
        from magent.launch import SESSION0_SERVE_REFUSAL

        _policy(monkeypatch, "refuse")
        plat = FakePlatform(interactive_session=False, supports_handoff=True)

        assert session0_refusal(plat, SESSION0_SERVE_REFUSAL) == SESSION0_SERVE_REFUSAL

    def test_no_desktop_overrides_even_the_serve_wording(self, monkeypatch):
        from magent.launch import SESSION0_SERVE_REFUSAL

        _policy(monkeypatch, "handoff")
        plat = FakePlatform(interactive_session=False, supports_handoff=False)

        assert "no user is logged on" in session0_refusal(plat, SESSION0_SERVE_REFUSAL)


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
    # schtasks is case-insensitive about its own switches, so the fake must be
    # too -- otherwise it pins a spelling instead of the recipe.
    lowered = [a.lower() for a in args]
    return args[lowered.index(flag) + 1] if flag in lowered else None


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
    """A recording ``schtasks`` that REALLY runs the launcher script.

    Installed by monkeypatching the ``_schtasks_exe`` SEAM, not by shadowing
    PATH: the real resolver reads the system directory first (an ssh login's
    PATH must not get to choose what runs as the logged-on user), so PATH
    shadowing would silently miss and write a real scheduled task.

    Not a mock of the module's own subprocess calls either. What is worth
    proving is that the generated PowerShell, its four result files and the
    exit-code round trip all work against a real process; a stubbed-out runner
    would prove only that the code calls functions. The fake owns the
    scheduler, never the launcher.

    The scratch ROOT is redirected into tmp_path as well. ``run_on_desktop``
    deliberately leaves its directory behind on failure and names it in
    ``detail``, so the tests that drive the failure paths would otherwise
    accumulate residue in the machine's real temp directory on every run.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    helper = bin_dir / "schtasks_helper.py"
    helper.write_text(_FAKE_SCHTASKS, encoding="utf-8")
    fake = bin_dir / "schtasks.cmd"
    fake.write_text(
        f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\nexit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("magent.platform.windows._schtasks_exe", lambda: str(fake))
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

    def test_a_logged_on_console_session_offers_the_mechanism(self, monkeypatch):
        monkeypatch.setattr(
            "magent.platform.windows.active_console_session_id", lambda: 1
        )

        assert self._plat().supports_desktop_handoff() is True

    @pytest.mark.parametrize("session", [0, 0xFFFFFFFF, None])
    def test_no_usable_console_session_withdraws_it(self, monkeypatch, session):
        # 0 is the services session, 0xFFFFFFFF means nothing is attached to
        # the console, None means the probe would not answer. None of the three
        # is a desktop, and a hand-off aimed at one would sit waiting out its
        # whole budget for a task Windows is never going to start.
        monkeypatch.setattr(
            "magent.platform.windows.active_console_session_id", lambda: session
        )

        assert self._plat().supports_desktop_handoff() is False

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
        assert modes == ["/Create", "/Run", "/Delete"]
        create = calls[0]
        task = create[create.index("/TN") + 1]
        assert task.startswith("magent-handoff-")
        # /IT is the whole point: "run only when the user is logged on" is what
        # puts the process in the interactive session, with no stored
        # credential. /F makes a re-run idempotent. /SC ONCE + /ST is the
        # trigger schtasks demands and /Run never waits for -- and 00:00 is
        # deliberately in the past, so a task stranded by a killed caller can
        # never fire on its own tonight.
        assert "/IT" in create
        assert "/F" in create
        assert create[create.index("/SC") + 1] == "ONCE"
        assert create[create.index("/ST") + 1] == "00:00"
        # No /RU and no /RP: a logged-on-only task needs no password and no
        # admin rights, and asking for either would make this unusable.
        assert "/RU" not in create
        assert "/RP" not in create
        # ...and the delete really names the same task, on every path.
        assert calls[-1] == ["/Delete", "/F", "/TN", task]

    def test_the_run_spec_is_a_fixed_launcher_under_the_limit(self, fake_schtasks):
        self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        create = _calls(fake_schtasks)[0]
        run_spec = create[create.index("/TR") + 1]
        # schtasks truncates /TR silently past 261 characters -- which is why
        # the task runs a SCRIPT FILE and not the real command line.
        assert len(run_spec) <= 261
        # powershell.exe, not pwsh: PowerShell 7 is not on every Windows box.
        assert run_spec.startswith("powershell.exe -NoProfile")
        assert "-ExecutionPolicy Bypass" in run_spec
        assert run_spec.endswith('run.ps1"')

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
        assert _calls(fake_schtasks)[-1][0] == "/Delete"
        # No kill: a bring-up still running on the desktop past our budget is
        # doing the work that was asked for, and the pid we hold is a number
        # Windows recycles freely.
        assert "may still be running" in result.detail

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

    def test_no_schtasks_is_a_named_failure_not_a_crash(self, monkeypatch):
        monkeypatch.setattr("magent.platform.windows._schtasks_exe", lambda: None)

        result = self._plat().run_on_desktop(["whatever"], timeout_s=5)

        assert result == HandoffResult(rc=None, detail="schtasks not found")

    def test_a_successful_handoff_leaves_no_scratch_behind(self, fake_schtasks):
        root = _scratch_root(fake_schtasks)

        self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        assert list(root.iterdir()) == []

    def test_the_scratch_root_does_not_grow_across_calls(self, fake_schtasks):
        # One directory per call, deleted on success -- so a machine that hands
        # off all day does not accumulate a launcher script per bring-up.
        for _ in range(3):
            self._plat().run_on_desktop([sys.executable, "-c", "pass"], timeout_s=60)

        assert list(_scratch_root(fake_schtasks).iterdir()) == []

    def test_a_failed_handoff_keeps_its_evidence(self, fake_schtasks, monkeypatch):
        # The other half of the same rule: on failure the directory STAYS and
        # is named in `detail`, because the launcher script and whatever the
        # command managed to write are the only evidence there is.
        monkeypatch.setenv("MDTEST_HANDOFF_NEVER_STARTS", "1")
        monkeypatch.setattr("magent.platform.windows._HANDOFF_START_GRACE_S", 0.2)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=30
        )

        left = list(_scratch_root(fake_schtasks).iterdir())
        assert len(left) == 1
        assert str(left[0]) in result.detail
        assert (left[0] / "run.ps1").is_file()

    def test_a_child_that_dies_without_an_exit_code_fails_fast(
        self, fake_schtasks, monkeypatch
    ):
        # pid.txt present, process gone, no rc.txt: the launcher lost its child
        # and nothing is ever going to write one. Reporting that now beats
        # spending the caller's whole 900s budget proving it.
        started = time.monotonic()
        monkeypatch.setattr("magent.platform.windows._read_pid", lambda _p: 4)
        monkeypatch.setattr("magent.platform.windows.pid_alive", lambda _p: False)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=60
        )

        assert result.rc is None
        assert result.timed_out is False
        assert "without an exit code" in result.detail
        assert time.monotonic() - started < 30


@pytestmark_win
class TestTheSchtasksResolver:
    """PATH is not this function's to trust: ``run_on_desktop`` is reached from
    an ssh login, and letting that login's PATH choose what runs as the
    logged-on user would turn a hand-off into an execution primitive."""

    def test_it_prefers_the_system_directory(self):
        from magent.platform import windows

        resolved = windows._schtasks_exe()

        assert resolved is not None
        assert Path(resolved).name.lower() == "schtasks.exe"
        assert Path(resolved).is_file()
        # System32 (or SysWOW64 under a 32-bit host), never a PATH entry
        # somebody else can write.
        assert Path(resolved).parent.name.lower() in ("system32", "syswow64")

    def test_a_path_plant_does_not_win(self, tmp_path, monkeypatch):
        plant = tmp_path / "evil"
        plant.mkdir()
        (plant / "schtasks.exe").write_text("not really", encoding="utf-8")
        monkeypatch.setenv("PATH", str(plant) + os.pathsep + os.environ.get("PATH", ""))

        from magent.platform import windows

        assert Path(windows._schtasks_exe()).parent != plant


class TestThePowerShellQuoting:
    """Two quoting layers stand between an argv and the desktop's child
    process, and a mistake in either splits an argument silently. Pure string
    assertions, so they run on every OS."""

    def test_a_literal_is_single_quoted_and_doubled(self):
        from magent.platform.windows import _ps_quote

        # Single quotes so nothing inside is expanded: these are paths and a
        # whole command line, and a `$` or a backtick must arrive verbatim.
        assert _ps_quote(r"C:\a $b `c") == r"'C:\a $b `c'"
        assert _ps_quote("it's") == "'it''s'"

    def test_the_argv_becomes_one_argument_list_string(self):
        from magent.platform.windows import _handoff_script

        script = _handoff_script(
            ["py.exe", "--config", r"C:\A B\magent.json", "up"],
            r"C:\work",
            Path("o"),
            Path("e"),
            Path("p"),
            Path("r"),
        )

        # list2cmdline quoted the space-bearing path, and _ps_quote then made
        # the WHOLE command line one PowerShell literal. Passing a list to
        # -ArgumentList instead would join with bare spaces and split that
        # path into two arguments.
        assert "-ArgumentList '--config \"C:\\A B\\magent.json\" up'" in script
        assert "-FilePath 'py.exe'" in script

    def test_it_carries_the_three_load_bearing_instructions(self):
        from magent.platform.windows import _handoff_script

        script = _handoff_script(
            ["py.exe"], r"C:\work", Path("o"), Path("e"), Path("p"), Path("r")
        )

        # No recursion: a hand-off that landed in Session 0 again refuses.
        assert "$env:MAGENT_SESSION0_POLICY = 'refuse'" in script
        # The caller's directory, because find_config walks up from the cwd and
        # a scheduled task starts in system32.
        assert "-WorkingDirectory 'C:\\work'" in script
        # No console flashed at the desktop by a command nobody typed.
        assert "-WindowStyle Hidden" in script

    def test_the_exit_code_is_written_last(self):
        from magent.platform.windows import _handoff_script

        script = _handoff_script(
            ["py.exe"], r"C:\work", Path("o"), Path("e"), Path("pid"), Path("rc")
        )

        # pid.txt is the "it really started" signal and must land first; rc.txt
        # is the completion signal and must land after WaitForExit, so a reader
        # that sees it can never read a half-written out.txt.
        assert script.index("'pid'") < script.index("WaitForExit")
        assert script.index("WaitForExit") < script.index("'rc'")

    def test_the_process_handle_is_cached_before_the_wait(self):
        from magent.platform.windows import _handoff_script

        script = _handoff_script(
            ["py.exe"], r"C:\work", Path("o"), Path("e"), Path("p"), Path("r")
        )

        # A `Start-Process -PassThru` object's .ExitCode is $null FOREVER
        # unless the handle is cached while the process is alive: PowerShell
        # does not hold it, so once the child exits there is nothing left to
        # ask. Measured, not theorised -- rc.txt came back empty on every run
        # until this line existed, and the whole hand-off then reported an
        # "unreadable exit code" for commands that had succeeded.
        assert "$null = $p.Handle" in script
        assert script.index("$p.Handle") < script.index("WaitForExit")


@pytestmark_win
class TestTheLauncherReallyRuns:
    """The generated PowerShell, executed for real, from a directory whose name
    has a space in it."""

    def test_a_path_with_spaces_survives_both_layers(self, tmp_path):
        from magent.platform.windows import _HANDOFF_SHELL, _handoff_script

        work = tmp_path / "a b c"
        work.mkdir()
        script = work / "run.ps1"
        out, err = work / "out.txt", work / "err.txt"
        pid, rc = work / "pid.txt", work / "rc.txt"
        script.write_text(
            _handoff_script(
                [sys.executable, "-c", "print('ok')"], str(work), out, err, pid, rc
            ),
            encoding="utf-8",
        )

        subprocess.run([*_HANDOFF_SHELL.split(), str(script)], check=True, timeout=120)

        assert out.read_text(encoding="utf-8").strip() == "ok"
        assert rc.read_text(encoding="utf-8").strip() == "0"
        assert pid.read_text(encoding="utf-8").strip().isdigit()
