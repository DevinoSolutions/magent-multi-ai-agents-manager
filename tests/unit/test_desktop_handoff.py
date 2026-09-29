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
from typing import ClassVar

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
from tests.unit._ps_parse import argument_of, parse_file
from tests.unit._ps_parse import parse as parse_powershell

# Every code point PowerShell's tokenizer ends a single-quoted literal on:
# U+0027 and the four typographic quotes U+2018, U+2019, U+201A and U+201B.
_PS_SINGLE_QUOTES = ["'", "\u2018", "\u2019", "\u201a", "\u201b"]


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

    def test_a_command_that_ran_is_never_reported_as_unrun(self, capsys):
        # The fourth answer is about a command that DID run on the desktop, so
        # the caller's own words must stay true for it -- exactly this line.
        detail = (
            "the desktop command finished but its exit code never became "
            "readable within 10.0s -- rc.txt was empty; task T, scratch left at S"
        )
        plat = FakePlatform(
            supports_handoff=True, handoff_result=HandoffResult(rc=None, detail=detail)
        )

        rc = relay_handoff(plat, ["x"], timeout_s=5)

        assert rc == 1
        assert capsys.readouterr().err.splitlines() == [
            f"  x hand-off failed: {detail} (see ~/.magent/logs/launch.log on this host)"
        ]


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
    if os.environ.get("MDTEST_HANDOFF_CREATE_FAILS") == "1":
        print("ERROR: Access is denied.", file=sys.stderr)
        sys.exit(1)
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
        # and /run stops being the fire-and-forget real schtasks is. And a cwd
        # of our own, because a real task starts in system32, never in the
        # caller's directory: a launcher that dropped -WorkingDirectory must
        # not pass by inheriting the right one.
        late = os.environ.get("MDTEST_HANDOFF_STARTS_LATE_S")
        if late:
            # Started, but slow to reach Start-Process (a loaded box): the
            # launcher is alive, so /Query says Running, and no pid.txt yet.
            spec = f'"{sys.executable}" -c "import time; time.sleep({late})" && {spec}'
        launcher = subprocess.Popen(
            spec,
            shell=True,
            cwd=here,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (here / f"{name}.pid").write_text(str(launcher.pid), encoding="utf-8")
elif mode == "/query":
    # Real schtasks prints a table; only the status word is ever read. Like
    # the real one it says Running while the task's launcher is alive, so a
    # short start grace abandons only a launcher that is gone.
    from magent.procs import pid_alive

    pid_file = here / f"{name}.pid"
    pid = int(pid_file.read_text(encoding="utf-8")) if pid_file.exists() else None
    print("TaskName   Next Run Time   Status")
    print(f"{name}   N/A   {'Running' if pid_alive(pid) else 'Ready'}")
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

    def test_a_non_ascii_working_directory_survives_the_script_file(
        self, fake_schtasks, tmp_path, monkeypatch
    ):
        # Windows PowerShell 5.1 reads a `-File` script with no BOM in the ANSI
        # code page, so a UTF-8 run.ps1 turned this directory into mojibake and
        # the desktop copy never started. The child prints ascii() of its cwd,
        # so its own console encoding cannot blur the comparison.
        where = tmp_path / "caf\u00e9 \u4e2d"
        where.mkdir()
        monkeypatch.chdir(where)
        # A launcher that never reaches Start-Process never writes pid.txt, so
        # a red run waits out the start grace: seconds, not the default 30.
        # Safe for a green one on a slow box, because the fake's /Query says
        # Running for as long as the launcher is alive.
        monkeypatch.setattr("magent.platform.windows._HANDOFF_START_GRACE_S", 5.0)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "import os; print(ascii(os.getcwd()))"],
            timeout_s=60,
        )

        assert result.rc == 0, result.detail
        assert result.stdout.strip() == ascii(str(where))

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

    def test_a_running_task_with_no_pid_yet_is_waited_for(
        self, fake_schtasks, monkeypatch
    ):
        # The other half of the start check: past the grace with no pid.txt,
        # but the scheduler says Running -- a slow launcher, not an empty
        # desktop, and abandoning it throws away a hand-off about to work. A
        # zero grace puts the /Query on the first polls, while the launcher is
        # still held back, so it is this branch that runs, on every run.
        monkeypatch.setenv("MDTEST_HANDOFF_STARTS_LATE_S", "2")
        monkeypatch.setattr("magent.platform.windows._HANDOFF_START_GRACE_S", 0.0)

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=60
        )

        assert result.rc == 0, result.detail
        modes = [c[0] for c in _calls(fake_schtasks)]
        assert modes == ["/Create", "/Run", "/Query", "/Delete"]

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
            result = self._plat().run_on_desktop(
                [sys.executable, "-c", "pass"], timeout_s=60
            )
            # Every call must have SUCCEEDED for "no growth" to mean anything:
            # a failed hand-off keeps its directory on purpose, so a harness
            # stall (the fake schtasks timing out under load) fails here, as
            # the failure it is, instead of below as "the root grew".
            assert result.rc == 0, result.detail

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
        # pid.txt and rc.txt share one reader; only the pid is faked.
        monkeypatch.setattr(
            "magent.platform.windows._read_recorded_int",
            lambda p: 4 if p.name == "pid.txt" else None,
        )
        monkeypatch.setattr("magent.platform.windows.pid_alive", lambda _p: False)
        # A gone pid is given a grace to still have its exit code written (the
        # launcher writes rc.txt after WaitForExit returns); shrink it so this
        # test proves the failure path, not the grace.
        monkeypatch.setattr("magent.platform.windows._HANDOFF_EXIT_GRACE_S", 0.5)
        # ...and the real launcher must not win the race by writing rc.txt: a
        # launcher that exits without writing one IS the lost-child case.
        monkeypatch.setattr(
            "magent.platform.windows._handoff_script", lambda *_a, **_k: "exit 0\n"
        )

        result = self._plat().run_on_desktop(
            [sys.executable, "-c", "pass"], timeout_s=60
        )

        assert result.rc is None
        assert result.timed_out is False
        assert "without an exit code" in result.detail
        assert time.monotonic() - started < 30


# CreateFileW arguments `_winapi` has no names for.
_CREATE_ALWAYS = 2
_FILE_SHARE_READ_WRITE = 0x1 | 0x2


class _PollClock:
    """The hand-off poll's ``time``, advanced only by the poll's own sleeps.

    Swapped in for ``magent.platform.windows.time`` so a scenario is keyed to
    POLL TICKS rather than to how loaded the machine is: every step the fake
    launcher takes lands between the same two reads on every run. Only the
    clock is fake -- the files the poll reads are real, and so is the lock.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.ticks = 0
        self.on_tick = lambda _tick: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.ticks += 1
        self.on_tick(self.ticks)


class _HeldExitCode:
    """rc.txt the way the launcher's ``Set-Content`` writes it: CREATED first,
    empty, and held open while the value goes in.

    ``locked`` holds it with no sharing, so a reader gets a sharing violation --
    which is what Windows PowerShell measurably does (298 of 300 first reads
    after the file appeared). ``shared`` holds a zero-byte file a reader CAN
    open, and reads as empty: the other half of the same window. ``partial``
    is ``shared`` with the first digit of a longer value already written and
    no line end yet. ``_winapi`` and not ``open()``, because Python's own open
    always shares.
    """

    def __init__(self, path: Path, mode: str) -> None:
        import _winapi

        self._winapi = _winapi
        share = 0 if mode == "locked" else _FILE_SHARE_READ_WRITE
        self._handle: int | None = _winapi.CreateFile(
            str(path), _winapi.GENERIC_WRITE, share, _winapi.NULL, _CREATE_ALWAYS, 0, 0
        )
        if mode == "partial":
            self.write("1")

    def write(self, text: str) -> None:
        """Write without closing -- the writer is still mid-value."""
        if self._handle is not None:
            self._winapi.WriteFile(self._handle, text.encode("ascii"))

    def finish(self, text: str) -> None:
        """Write the rest and close -- the moment Set-Content returns."""
        self.write(text)
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            self._winapi.CloseHandle(self._handle)
            self._handle = None


@pytestmark_win
class TestTheExitCodeIsFinalOnlyAsAnInteger:
    """rc.txt EXISTING is not the exit code being WRITTEN.

    The launcher's ``Set-Content`` creates rc.txt before it writes the value
    and holds it while it does, so a poll can see the file and read nothing.
    That read used to be terminal -- ``rc=None``, "unreadable exit code ''" --
    for a command that had succeeded: ``assert None == 7`` in
    ``test_the_command_really_runs_and_its_streams_come_back``, on five CI runs
    of unrelated PRs. The pid.txt read already knew that "present but not an
    integer" means "not yet"; both now also require the line end Set-Content
    writes after the value, so a prefix is never final either.

    Driven through the poll directly, against real files, with no scheduler at
    all: the window is opened on purpose and held for a known number of poll
    ticks, not hoped for.
    """

    @pytest.fixture
    def handoff(self, tmp_path, monkeypatch):
        work = tmp_path / "scratch"
        work.mkdir()
        files = (
            work / "out.txt",
            work / "err.txt",
            work / "pid.txt",
            work / "rc.txt",
        )
        files[0].write_text("hello out\n", encoding="utf-8")
        files[1].write_text("hello err\n", encoding="utf-8")
        files[2].write_text("4242\n", encoding="utf-8")
        # The child is gone -- the launcher writes rc.txt only after
        # WaitForExit -- which is exactly the state a poll meets mid-write.
        monkeypatch.setattr("magent.platform.windows.pid_alive", lambda _p: False)
        clock = _PollClock()
        monkeypatch.setattr("magent.platform.windows.time", clock)
        return work, files, clock

    @pytest.fixture
    def hold(self, request):
        def _hold(path: Path, mode: str) -> _HeldExitCode:
            held = _HeldExitCode(path, mode)
            request.addfinalizer(held.close)
            return held

        return _hold

    def _await(self, work, files, timeout_s):
        from magent.platform.windows import WindowsPlatform

        # pid.txt is present, so the start check -- the only schtasks call the
        # poll makes -- is never reached.
        return WindowsPlatform()._await_handoff(
            "schtasks-is-never-asked", "magent-handoff-test", work, files, timeout_s
        )

    @pytest.mark.parametrize("mode", ["locked", "shared"])
    def test_an_exit_code_still_being_written_is_waited_for(self, handoff, hold, mode):
        work, files, clock = handoff
        rc_file = files[3]
        held = hold(rc_file, mode)
        # The window is real before the poll starts: the file is there, and it
        # does not read as an exit code.
        assert rc_file.exists()
        if mode == "locked":
            with pytest.raises(PermissionError):
                rc_file.read_text(encoding="utf-8")
        else:
            assert rc_file.read_text(encoding="utf-8") == ""

        def launcher(tick: int) -> None:
            # Set-Content returns three poll ticks after it created the file.
            if tick == 3:
                held.finish("7\r\n")

        clock.on_tick = launcher

        result = self._await(work, files, timeout_s=60)

        assert result.rc == 7
        assert result.timed_out is False
        assert result.detail == ""
        assert "hello out" in result.stdout
        assert "hello err" in result.stderr
        # It really sat through the window: three reads met the held file.
        assert clock.ticks >= 3

    def test_an_exit_code_that_lands_as_the_budget_ends_still_counts(
        self, handoff, hold
    ):
        work, files, clock = handoff
        held = hold(files[3], "locked")

        def launcher(tick: int) -> None:
            # Set-Content returns during the poll's LAST sleep.
            if tick == 2:
                held.finish("7\r\n")

        clock.on_tick = launcher

        result = self._await(work, files, timeout_s=0.5)

        assert clock.now >= 0.5
        assert result.rc == 7
        assert result.detail == ""

    def test_an_exit_code_that_lands_after_the_last_read_still_counts(
        self, handoff, hold, monkeypatch
    ):
        # The budget runs out with rc.txt still held at the poll's final read,
        # and Set-Content returns an instant later. The read the poll gives up
        # with is decisive: a code complete by then is the answer, not
        # something to print in `detail` and throw away.
        from magent.platform import windows

        work, files, clock = handoff
        held = hold(files[3], "locked")
        real_read = windows._read_recorded_int

        def poll_read(path: Path) -> int | None:
            value = real_read(path)
            if path == files[3] and clock.now >= 0.5:
                held.finish("7\r\n")
            return value

        monkeypatch.setattr("magent.platform.windows._read_recorded_int", poll_read)

        result = self._await(work, files, timeout_s=0.5)

        assert result.rc == 7, result.detail
        assert result.detail == ""
        # A full success, not a success-shaped report: the scratch directory
        # goes, like on any hand-off that got its exit code back.
        assert not work.exists()

    def test_a_partial_exit_code_is_not_yet_an_answer(self, handoff, hold):
        # The "1" of "12": a value without the line end Set-Content writes
        # after it may be a prefix, and a prefix must never be final.
        work, files, clock = handoff
        rc_file = files[3]
        held = hold(rc_file, "partial")
        assert rc_file.read_text(encoding="utf-8") == "1"

        def launcher(tick: int) -> None:
            if tick == 3:
                held.finish("2\r\n")

        clock.on_tick = launcher

        result = self._await(work, files, timeout_s=60)

        assert result.rc == 12, result.detail
        assert clock.ticks >= 3

    def test_a_present_exit_code_is_never_mistaken_for_a_lost_child(
        self, handoff, hold
    ):
        from magent.platform.windows import _HANDOFF_EXIT_GRACE_S, _HANDOFF_POLL_S

        work, files, clock = handoff
        # The child died at t=0. rc.txt appears just inside the exit grace and
        # Set-Content returns just after it -- the loaded-runner shape that
        # grace exists for. A poll that ran the pid checks while the file was
        # there would call this a lost child at the grace, mid-write.
        created = round((_HANDOFF_EXIT_GRACE_S - 0.5) / _HANDOFF_POLL_S)
        held: list[_HeldExitCode] = []

        def launcher(tick: int) -> None:
            if tick == created:
                held.append(hold(files[3], "locked"))
            if tick == created + 4:
                held[0].finish("7\r\n")

        clock.on_tick = launcher

        result = self._await(work, files, timeout_s=60)

        assert result.rc == 7, result.detail
        assert clock.now > _HANDOFF_EXIT_GRACE_S

    # What the last read saw, in our words: a refused read, a launcher that
    # wrote no value, and a value cut short are different bugs. "Refused", not
    # "held by the writer": errno 13 is also an ACL denial or a delete-pending
    # file, so the words claim no more than the error does.
    _SEEN: ClassVar[dict[str, str]] = {
        "locked": "rc.txt was locked or refused (PermissionError)",
        "shared": "rc.txt was empty",
        "partial": "rc.txt held '1', not a complete exit code",
    }

    @pytest.mark.parametrize("mode", ["locked", "shared", "partial"])
    def test_an_exit_code_that_never_becomes_readable_is_its_own_answer(
        self, handoff, hold, mode
    ):
        from magent.platform.windows import _HANDOFF_RC_GRACE_S

        work, files, clock = handoff
        hold(files[3], mode)  # ...and never finishes.

        result = self._await(work, files, timeout_s=60)

        self._assert_unreadable_answer(result, work, mode, _HANDOFF_RC_GRACE_S)
        # A short grace, not the caller's whole budget.
        assert clock.now < 60

    @pytest.mark.parametrize("mode", ["locked", "shared", "partial"])
    def test_a_budget_that_runs_out_mid_write_gets_the_same_answer(
        self, handoff, hold, mode
    ):
        work, files, clock = handoff
        hold(files[3], mode)

        result = self._await(work, files, timeout_s=0.5)

        self._assert_unreadable_answer(result, work, mode, 0.5)
        assert clock.now >= 0.5

    def test_the_os_words_go_to_the_log_never_the_screen(
        self, handoff, hold, caplog, capsys
    ):
        # What the OS said about the refused read is worth keeping -- in
        # launch.log, where a bug report can quote it. The screen gets our
        # words and the exception class: in `detail`, and in the line the
        # relay prints from it.
        work, files, _ = handoff
        hold(files[3], "locked")
        with pytest.raises(PermissionError) as refused:
            files[3].read_text(encoding="utf-8")
        os_words = refused.value.strerror
        assert os_words

        with caplog.at_level("WARNING", logger="magent.launch"):
            result = self._await(work, files, timeout_s=60)

        logged = [r for r in caplog.records if r.name == "magent.launch"]
        assert [r.levelname for r in logged] == ["WARNING"]
        assert "rc.txt unreadable" in logged[0].getMessage()
        assert os_words in logged[0].getMessage()
        assert os_words not in result.detail

        relay_handoff(
            FakePlatform(supports_handoff=True, handoff_result=result),
            ["x"],
            timeout_s=5,
        )
        screen = capsys.readouterr()
        assert result.detail in screen.err
        assert os_words not in screen.out + screen.err

    def _assert_unreadable_answer(self, result, work, mode, waited):
        assert result.rc is None
        # rc.txt exists only after WaitForExit, so the command FINISHED: not
        # "may still be running", and no fabricated exit code either.
        assert result.timed_out is False
        assert f"never became readable within {waited:.1f}s -- " in result.detail
        assert self._SEEN[mode] in result.detail
        # Our words on screen; the OS's own text goes to the log.
        assert "Permission denied" not in result.detail
        # ...and none of the other three answers.
        assert "never started" not in result.detail
        assert "without an exit code" not in result.detail
        assert "may still be running" not in result.detail
        # Its output is complete by now, so it is relayed with the failure,
        # and the scratch directory is kept and named as the evidence.
        assert "hello out" in result.stdout
        assert "hello err" in result.stderr
        assert work.exists()
        assert f"scratch left at {work}" in result.detail


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


@pytestmark_win
class TestThePowerShellQuoting:
    """Two quoting layers stand between an argv and the desktop's child
    process, and a mistake in either splits an argument silently. Pure string
    assertions -- but they live in ``platform/windows.py``, which imports
    ``ctypes.WINFUNCTYPE`` at module level and so cannot be imported anywhere
    else (CI proved it: 5 ImportErrors on every macOS/Linux leg)."""

    def test_a_literal_is_single_quoted_and_doubled(self):
        from magent.platform.windows import _ps_quote

        # Single quotes so nothing inside is expanded: these are paths and a
        # whole command line, and a `$` or a backtick must arrive verbatim.
        assert _ps_quote(r"C:\a $b `c") == r"'C:\a $b `c'"
        assert _ps_quote("it's") == "'it''s'"

    @pytest.mark.parametrize("quote", _PS_SINGLE_QUOTES)
    def test_every_single_quote_powershell_knows_is_doubled(self, quote):
        from magent.platform.windows import _ps_quote

        # PowerShell ends a single-quoted literal on any of FIVE code points,
        # not only the ASCII one, and doubling is the escape for each.
        assert _ps_quote(f"a{quote}b") == f"'a{quote}{quote}b'"

    @pytest.mark.parametrize("quote", _PS_SINGLE_QUOTES)
    def test_a_cwd_holding_a_single_quote_stays_one_literal(self, tmp_path, quote):
        # PowerShell's own parser, never a run: the whole cwd must come back as
        # the ONE single-quoted -WorkingDirectory value, and no fragment of it
        # may parse as a command of its own.
        # Tokenizer layer only (parse() over text we decoded ourselves); the
        # file-decoding layer is ParseFile's every-single-quote/cwd-non-ascii-quote.
        from magent.platform.windows import _handoff_script

        cwd = rf"C:\work{quote}; Get-Date; {quote}x"
        script = _handoff_script(
            ["py.exe", "up"], cwd, Path("o"), Path("e"), Path("p"), Path("r")
        )
        parsed = parse_powershell(script, tmp_path)
        assert parsed.errors == []
        assert parsed.named("Get-Date") == []
        (start,) = parsed.named("Start-Process")
        assert argument_of(start, "WorkingDirectory") == ("const", "SingleQuoted", cwd)

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
    has spaces and non-ASCII in it -- so a script staged in any encoding but
    production's cannot pass."""

    def test_a_path_with_spaces_survives_both_layers(self, tmp_path):
        from magent.platform.windows import (
            _HANDOFF_SCRIPT_ENCODING,
            _HANDOFF_SHELL,
            _handoff_script,
        )

        work = tmp_path / "a b Ñ т"
        work.mkdir()
        script = work / "run.ps1"
        out, err = work / "out.txt", work / "err.txt"
        pid, rc = work / "pid.txt", work / "rc.txt"
        # Staged exactly as run_on_desktop stages it, encoding included.
        script.write_text(
            _handoff_script(
                [sys.executable, "-c", "print('ok')"], str(work), out, err, pid, rc
            ),
            encoding=_HANDOFF_SCRIPT_ENCODING,
        )

        subprocess.run([*_HANDOFF_SHELL.split(), str(script)], check=True, timeout=120)

        assert out.read_text(encoding="utf-8").strip() == "ok"
        assert rc.read_text(encoding="utf-8").strip() == "0"
        assert pid.read_text(encoding="utf-8").strip().isdigit()


_PY = r"C:\Python\python.exe"

# (argv, name of the caller's directory). Every case carries U+0442 or U+4E2D,
# which a cp1252 writer cannot encode at all and which a script without a BOM
# turns into mojibake under -File -- so each one is red against both writers.
_STAGED_CASES = {
    "non-ascii-argv": (
        ["C:\\Caf\u00e9\\python.exe", "-m", "magent", "up", "caf\u00e9 \u00d1 \u0442"],
        "work",
    ),
    "every-single-quote": (
        [_PY, "-m", "magent", "up", "a'b\u2018c\u2019d\u201ae\u201bf \u0442"],
        "work",
    ),
    "config-typographic-quote": (
        [_PY, "-m", "magent", "--config", "C:\\A\u2019B\\\u4e2d\\magent.json", "up"],
        "work",
    ),
    "config-non-ascii-space": (
        [_PY, "-m", "magent", "--config", "C:\\\u00d1 \u0442\\magent.json", "up"],
        "work",
    ),
    "cwd-non-ascii-quote": ([_PY, "-m", "magent", "up"], "\u00d1\u2019s \u0442"),
}


@pytestmark_win
class TestTheStagedScriptParsesBackToTheArgv:
    """The run.ps1 ``run_on_desktop`` REALLY stages, read the way -File reads it.

    ``ParseFile`` decodes a file exactly as ``powershell.exe -File`` does and
    executes nothing, so this parses the bytes production wrote rather than
    the text we meant them to hold. /Create fails, so the scratch directory is
    kept as evidence and nothing runs. Windows PowerShell 5.1 reads a script
    with no BOM in the ANSI code page, where the UTF-8 bytes of U+00D1 and
    U+0442 each hold a typographic single quote -- and that parses with NO
    error and different values, which is why every literal is compared, not
    just the error count.

    Blind spot: a machine whose ANSI code page is 65001 (UTF-8) reads a script
    with no BOM as UTF-8 too, so there these pins pass against a BOM-less
    writer as well. They discriminate wherever the ANSI code page is not
    UTF-8 -- cp1252 on windows-latest -- and the decoding check below skips,
    saying so, on a 65001 machine.
    """

    def test_its_parser_decodes_a_file_the_way_dash_file_does(self, tmp_path):
        # The pins below are only as good as this parser's DECODING. ParseInput
        # over text we decoded ourselves, or a PowerShell 7 host (UTF-8 when
        # there is no BOM), would read both of these files alike -- and pass
        # the very writer the pins exist to catch.
        import ctypes

        value = "café"
        ansi = value.encode("utf-8").decode(f"cp{ctypes.windll.kernel32.GetACP()}")
        if ansi == value:
            pytest.skip("the ANSI code page is UTF-8: a BOM changes nothing here")
        seen = {}
        for encoding in ("utf-8", "utf-8-sig"):
            src = tmp_path / f"{encoding}.ps1"
            src.write_text(f"Set-Content -LiteralPath '{value}'\n", encoding=encoding)
            (command,) = parse_file(src, tmp_path).named("Set-Content")
            seen[encoding] = argument_of(command, "LiteralPath")[-1]

        # No BOM: the ANSI code page, as -File reads it. A BOM: honoured.
        assert seen == {"utf-8": ansi, "utf-8-sig": value}

    @pytest.mark.parametrize(
        ("argv", "cwd_name"), list(_STAGED_CASES.values()), ids=list(_STAGED_CASES)
    )
    def test_every_literal_is_what_was_asked_for(
        self, fake_schtasks, tmp_path, monkeypatch, argv, cwd_name
    ):
        from magent.platform.windows import WindowsPlatform

        monkeypatch.setenv("MDTEST_HANDOFF_CREATE_FAILS", "1")
        # The redirects and both Set-Content targets live under the scratch
        # root, so a non-ASCII root puts them through the same encoding.
        root = tmp_path / "T\u00ebmp \u00d1 \u0442"
        root.mkdir()
        monkeypatch.setattr(
            "magent.platform.windows.tempfile.gettempdir", lambda: str(root)
        )
        cwd = tmp_path / cwd_name
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        result = WindowsPlatform().run_on_desktop(argv, timeout_s=60)

        # Staged, then refused: the task was never run, only cleaned up.
        assert result.rc is None
        assert "schtasks /Create exited 1" in result.detail
        assert [c[0] for c in _calls(fake_schtasks)] == ["/Create", "/Delete"]
        (work,) = (root / "magent-handoff").iterdir()
        assert str(work) in result.detail

        parsed = parse_file(work / "run.ps1", tmp_path)

        assert parsed.errors == []
        # No fragment of any value parsed as a command of its own.
        assert [c[0][-1] for c in parsed.commands] == [
            "Start-Process",
            "Set-Content",
            "Set-Content",
        ]
        (start,) = parsed.named("Start-Process")
        expected = {
            "FilePath": argv[0],
            # ONE literal holding the whole command line, not a list.
            "ArgumentList": subprocess.list2cmdline(argv[1:]),
            "WorkingDirectory": str(cwd),
            "RedirectStandardOutput": str(work / "out.txt"),
            "RedirectStandardError": str(work / "err.txt"),
        }
        assert {name: argument_of(start, name) for name in expected} == {
            name: ("const", "SingleQuoted", value) for name, value in expected.items()
        }
        # pid.txt first, rc.txt last.
        assert [argument_of(c, "LiteralPath") for c in parsed.named("Set-Content")] == [
            ("const", "SingleQuoted", str(work / "pid.txt")),
            ("const", "SingleQuoted", str(work / "rc.txt")),
        ]
