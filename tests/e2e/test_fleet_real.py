"""`magent send` / `model` / `peek` / `sessions --json` against a REAL multiplexer.

The unit tier (``tests/unit/test_fleet_cmd.py``) drives these commands against
``tests/unit/_fake_psmux.py`` -- a genuine on-disk binary that RECORDS argv but
is not a terminal multiplexer. So the argv magent builds is pinned and the WIRE
is not: nothing there proves that ``send-keys -l`` + a separate ``Enter``
actually puts text into a live pane, that a footer painted by a program comes
back through ``capture-pane`` parseable, or that ``--wait-idle`` waits for a
real mid-turn agent rather than racing it.

This tier closes that. Every session here is a real detached multiplexer session
hosting ``_fleet_agent.py`` -- a stand-in that imitates Claude Code's on-screen
contract (the ``U+276F`` input line, the ``U+00B7`` footer, a hints row BELOW
the footer) and appends every line it reads to a JSON log. That log, not a
screen scrape, is the ground truth for "the text arrived verbatim": magent is
driven as a real subprocess (``python -m magent --config ... send ...``) and
what it delivers is read off disk.

What is real: the multiplexer, the session, the pane, the pty, ``send-keys
-l``, the ``Enter``, ``capture-pane``, the footer parse, the busy/idle
classification, the CLI process and its exit code. What is substituted: the
agent itself (a stand-in, since a real Claude Code pane needs credentials, a
network and a model) and -- on POSIX only -- the multiplexer BINARY.

HONEST GAP, stated the way CLAUDE.md states it for the browser tier: Linux and
macOS have no ``psmux`` binary, so real ``tmux`` is symlinked in as ``psmux`` on
a tmp PATH with ``TMUX_TMPDIR`` confined to a private 0700 directory. That makes
creation, probing, injection and capture genuinely exercise a live multiplexer;
it does not prove psmux's own Windows-specific behaviour, which is exactly what
the Windows leg (real psmux 3.3.8, provisioned by ``.github/actions/
install-psmux``) is for. A missing multiplexer therefore FAILS on CI and only
skips locally -- a runner without one is a provisioning bug, never a skip.

Three measured facts about the multiplexers shape the harness, the first two
verified on this repo's Windows box against real psmux 3.3.8 and the third on a
macOS CI runner:

* **A pane command is read by a SHELL, not exec'd.** ``new-session ... <python>
  <script>`` with a script path containing a space and an ``&`` came up as a
  pwsh background job rather than the script. So the pane command is ONE
  shell-quoted argument (see :func:`_pane_command`) pointing at a launcher shim
  -- the same device ``tests/unit/_fake_psmux.py`` uses, and the reason this
  tier is immune to a temp directory with a space in it.
* **A detached pane does not echo, and the Windows console over-echoes.** See
  ``_fleet_agent``'s module docstring: the stand-in owns its input line in raw
  mode, like the real TUI does.
* **A tmux socket does not fit under pytest's tmp_path on macOS.** A UNIX socket
  path is capped at ~104 bytes there; pytest's tmp root made it 183 and every
  ``new-session`` failed with "File name too long". So ``TMUX_TMPDIR`` lives in a
  short ``/tmp/mgf-XXXXXXXX`` and the session names are short too -- see
  :func:`_resolve_multiplexer`.
"""

from __future__ import annotations

import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.e2e._pty import Budget

if TYPE_CHECKING:
    from typing import NoReturn

pytestmark = [pytest.mark.e2e]

_AGENT = Path(__file__).parent / "_fleet_agent.py"
_CARET = "\u276f"
_MIDDOT = "\u00b7"

# A whole-test wall clock, the same doctrine as the pty tiers: per-step timeouts
# answer "how long may THIS step take", never "how long may the test take", and
# a test that outlives its CI job's timeout-minutes is CANCELLED -- which throws
# away the pytest summary and every result after it.
_BUDGET_S = 120.0

# The stand-in's mid-turn windows. Long enough that a poll can observe `busy`
# and that a wait is provably a wait, short enough to fit several in one budget.
_COMPACT_S = 3.0
_BUSY_S = 10.0
_HOLD_S = 12.0

# How long the fleet gets to come up before a test runs.
_READY_S = 45.0


def _missing_multiplexer(name: str) -> NoReturn:
    """No multiplexer: a provisioning bug on CI, a plain skip on a dev box."""
    message = (
        f"{name} not found. On CI this tier must never skip -- the Windows leg is"
        " provisioned by .github/actions/install-psmux and the POSIX legs by the"
        " end-to-end job's tmux step."
    )
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(message)
    pytest.skip(message)


def _resolve_multiplexer(tmp_path: Path) -> tuple[str, dict[str, str], str | None]:
    """``(binary, extra child env, socket dir to clean up)``.

    Real psmux on Windows; on POSIX the browser tier's device copied faithfully
    (``test_upload_browser._BrowserServe._install_psmux``): a symlink named
    ``psmux`` in a tmp bindir that is PREPENDED to PATH, so the magent
    subprocess's own ``find_psmux()`` resolves it, plus a private 0700
    ``TMUX_TMPDIR`` so no socket of this tier's can collide with a real one.

    That socket directory is deliberately NOT under ``tmp_path``, and moving it
    back would break macOS only: a UNIX socket path is capped at the ~104 bytes
    of ``sockaddr_un.sun_path`` (108 on Linux), and pytest's own tmp root is
    long enough to blow it on its own -- measured on a macOS runner, the socket
    came to 183 characters under ``/private/var/folders/...
    /pytest-of-runner/pytest-0/<test name>0/tmux/tmux-501/<session>`` and every
    ``new-session`` failed with "File name too long". Linux survived the same
    layout purely because ``/tmp/pytest-of-runner/...`` is shorter, and Windows
    because psmux uses named pipes. So the socket dir is a short
    ``/tmp/mgf-XXXXXXXX`` and the session names are short too (the socket FILE
    name counts toward the same budget); everything that does not go in a
    ``sun_path`` -- logs, config, launcher shims -- stays under ``tmp_path``.
    """
    if sys.platform == "win32":
        from magent import psmux

        binary = psmux.find_psmux()
        if not binary:
            _missing_multiplexer("psmux")
        return str(binary), {}, None

    tmux = shutil.which("tmux")
    if not tmux:
        _missing_multiplexer("tmux (the POSIX psmux stand-in)")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "psmux"
    os.symlink(str(tmux), link)
    # mkdtemp is already 0700, which tmux REQUIRES of its socket dir. `/tmp` is
    # the shortest directory both POSIX runners have (and on macOS it resolves
    # to the equally short `/private/tmp`).
    root = "/tmp" if Path("/tmp").is_dir() else None
    tmux_tmp = tempfile.mkdtemp(prefix="mgf-", dir=root)
    return (
        str(link),
        {
            "TMUX_TMPDIR": tmux_tmp,
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        },
        tmux_tmp,
    )


def _pane_command(script: Path) -> str:
    """One shell-command string that runs ``script`` as the pane's process.

    Both multiplexers hand a command to a shell when it arrives as a single
    argument, and psmux was measured doing it even for a MULTI-argument one --
    pwsh interpreted a path containing ``&`` as a background-job operator. A
    single, correctly quoted argument is the only form that is safe on both:
    pwsh's call operator on Windows, ``shlex.quote`` on POSIX.
    """
    if sys.platform == "win32":
        return "& '" + str(script).replace("'", "''") + "'"
    return shlex.quote(str(script))


@functools.lru_cache(maxsize=1)
def _posix_shell() -> str | None:
    """A shell whose ARGUMENT HANDLING is the thing under test, or None.

    On Windows that must be Git Bash (MSYS), not WSL's ``C:\\Windows\\system32\\
    bash.exe`` -- and ``shutil.which("bash")`` finds the latter first on this
    box. Two measured reasons it cannot stand in: only the MSYS runtime rewrites
    ``/compact`` into ``C:/Program Files/Git/compact`` (the rewrite this test
    exists to survive), and WSL cannot see a ``C:/...`` path at all -- it wants
    ``/mnt/c/...``, so every command died with "No such file or directory".
    Flavour is settled by asking ``uname -s`` rather than by guessing from the
    path, so a Git Bash installed anywhere still qualifies.
    """
    candidates = [shutil.which("bash")]
    if sys.platform == "win32":
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
        git = shutil.which("git")
        if git:
            candidates.append(str(Path(git).parent.parent / "bin" / "bash.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            uname = subprocess.run(
                [candidate, "-c", "uname -s"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout.upper()
        except (OSError, subprocess.SubprocessError):
            continue
        if sys.platform != "win32":
            return candidate
        if any(flavour in uname for flavour in ("MINGW", "MSYS", "CYGWIN")):
            return candidate
    return None


def _bashify(arg: str) -> str:
    """A Windows path made runnable BY bash, everything else left alone.

    bash only treats a word as a path when it contains a ``/`` -- a quoted
    ``C:\\...\\python.exe`` is a COMMAND NAME to look up on PATH, and the lookup
    fails with "command not found" (measured). Forward slashes fix that, and
    MSYS leaves a drive-lettered path alone rather than rewriting it, so the
    path conversion this test is ABOUT still applies only to the ``/``-leading
    arguments it is about.
    """
    if sys.platform == "win32" and len(arg) > 2 and arg[1:3] == ":\\":
        return arg.replace("\\", "/")
    return arg


def _wait_until(check, timeout: float, interval: float = 0.25):
    """Poll ``check`` until it is truthy or ``timeout`` runs out; return the last
    value either way, so the caller's assertion reports the real state."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


class _Fleet:
    """Two live stand-in panes plus one configured-but-never-started project."""

    def __init__(self, tmp_path: Path, budget: Budget) -> None:
        self.budget = budget
        self.binary, extra_env, self.socket_dir = _resolve_multiplexer(tmp_path)
        # UNIQUE per run, and that is a safety property rather than tidiness:
        # this repo's dev box carries dozens of live psmux sessions, and a name
        # collision would make `kill-server` in teardown stop a real agent.
        # SHORT, because on POSIX the name is also the socket's file name and
        # counts toward the sun_path budget -- see _resolve_multiplexer. Still
        # an obviously non-production shape, and `psmux.session_name()` maps it
        # to itself (it only rewrites "." ":" and " ").
        stem = f"mgf-{uuid.uuid4().hex[:8]}"
        self.alpha = f"{stem}-a"
        self.beta = f"{stem}-b"
        self.dead = f"{stem}-d"
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.logs: dict[str, Path] = {}
        self.created: list[str] = []

        # The autouse _isolate_magent_home fixture has already pointed the whole
        # HOME family at tmp, so this inherits a redirected ~ (and ~/.psmux)
        # rather than rebuilding one -- conftest's guard B fails any child env
        # that aims HOME/USERPROFILE at the real home.
        self.env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        # The four test-isolation laws, every one of which reaches past a HOME
        # redirect if left at its default (CLAUDE.md: supervisors, the
        # image-name priority sweep, the Session-0 desktop hand-off).
        self.env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
        self.env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
        self.env["MAGENT_PSMUX_BOOST"] = "0"
        self.env["MAGENT_SESSION0_POLICY"] = "allow"
        self.env.update(extra_env)

        self.projdirs = {}
        for name in (self.alpha, self.beta, self.dead):
            d = tmp_path / f"proj-{name}"
            d.mkdir()
            self.projdirs[name] = d

        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        # The TITLE is the session name: psmux.session_name() only
                        # rewrites "." ":" and " ", so these names are fixed
                        # points and config -> socket_id -> live_sessions resolves
                        # them without a mapping step.
                        {
                            "path": str(self.projdirs[name]),
                            "title": name,
                            "tool": "probe",
                        }
                        for name in (self.alpha, self.beta, self.dead)
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": "rem mgfleet-never-run"},
                        "uploadServer": False,
                    },
                }
            ),
            encoding="utf-8",
        )

        for name in (self.alpha, self.beta):
            self._start(name, tmp_path)

    # -- the multiplexer ------------------------------------------------------

    def psmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            check=False,
            timeout=max(5.0, self.budget.clamp(30.0)),
        )

    def capture(self, name: str) -> str:
        """The pane's visible text, read exactly as ``psmux.capture_pane`` does."""
        return self.psmux("-L", name, "capture-pane", "-p", "-t", name).stdout

    def _start(self, name: str, tmp_path: Path) -> None:
        log = tmp_path / f"{name}.jsonl"
        self.logs[name] = log
        shim = tmp_path / f"run-{name}{'.cmd' if sys.platform == 'win32' else '.sh'}"
        argv = [
            sys.executable,
            str(_AGENT),
            "--log",
            str(log),
            "--name",
            name,
            "--compact-seconds",
            str(_COMPACT_S),
        ]
        if sys.platform == "win32":
            shim.write_text(
                "@echo off\r\n" + subprocess.list2cmdline(argv) + "\r\n",
                encoding="utf-8",
            )
        else:
            shim.write_text(
                "#!/bin/sh\nexec " + shlex.join(argv) + "\n", encoding="utf-8"
            )
            shim.chmod(0o755)

        result = self.psmux(
            "-L",
            name,
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            str(self.projdirs[name]),
            _pane_command(shim),
        )
        assert result.returncode == 0, (
            f"new-session {name} failed rc={result.returncode}: {result.stderr}"
        )
        self.created.append(name)

    def _live(self, name: str) -> bool:
        return self.psmux("-L", name, "has-session", "-t", name).returncode == 0

    def _painted(self, name: str) -> str:
        """The pane once it shows BOTH an input line and a footer, else ``""``.

        Both, because either one alone is a half-started pane: the caret proves
        the stand-in is painting and the footer is what every state read parses.
        """
        pane = self.capture(name)
        return pane if (_CARET in pane and _MIDDOT in pane) else ""

    def wait_ready(self) -> None:
        """Block until both panes have painted their input line AND footer."""
        for name in (self.alpha, self.beta):
            assert _wait_until(
                lambda n=name: self._live(n), self.budget.clamp(_READY_S)
            ), f"session {name} never came up"
            assert _wait_until(
                lambda n=name: self._painted(n), self.budget.clamp(_READY_S)
            ), (
                f"{name}'s pane never painted an input line + footer. "
                f"capture:\n{self.capture(name)!r}"
            )

    # -- ground truth ---------------------------------------------------------

    def received(self, name: str) -> list[str]:
        """Every line the stand-in has read, in order -- the wire's own record."""
        log = self.logs[name]
        if not log.exists():
            return []
        return [
            json.loads(raw)["line"]
            for raw in log.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    # -- the product ----------------------------------------------------------

    def cli(
        self, *args: str, timeout: float = 90.0
    ) -> subprocess.CompletedProcess[str]:
        """Run the REAL CLI as its own process; `--config` is a group option."""
        return subprocess.run(
            [sys.executable, "-m", "magent", "--config", str(self.cfg), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            cwd=str(self.work),
            check=False,
            timeout=max(5.0, self.budget.clamp(timeout)),
        )

    def cli_via_bash(
        self, *args: str, timeout: float = 90.0
    ) -> subprocess.CompletedProcess[str] | None:
        """The same command through a POSIX shell, MSYS path conversion ON.

        Returns None when no ``bash`` resolves (then the caller skips only that
        sub-assertion). Deliberately WITHOUT ``MSYS_NO_PATHCONV``: the point is
        that magent's slash commands survive a shell that rewrites anything
        looking like a POSIX path.
        """
        bash = _posix_shell()
        if not bash:
            return None
        argv = [
            _bashify(a)
            for a in (sys.executable, "-m", "magent", "--config", str(self.cfg), *args)
        ]
        return subprocess.run(
            [bash, "-c", shlex.join(argv)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            cwd=str(self.work),
            check=False,
            timeout=max(5.0, self.budget.clamp(timeout)),
        )

    def sessions(self) -> dict[str, dict[str, object]]:
        """``sessions --json`` keyed by name, read from STDOUT only (a stderr
        diagnostic must never end up inside json.loads -- NF-S3-002)."""
        result = self.cli("sessions", "--json")
        assert result.returncode == 0, f"sessions --json failed: {result.stderr}"
        return {row["name"]: row for row in json.loads(result.stdout)}

    # -- teardown -------------------------------------------------------------

    def teardown(self) -> list[str]:
        """Kill ONLY the servers this fleet created. Never `magent down`, never
        `psmux.kill_servers` -- nothing here may reach a session it did not make."""
        leftovers = []
        for name in self.created:
            self.psmux("-L", name, "kill-server")
        for name in self.created:
            if not _wait_until(lambda n=name: not self._live(n), 10.0):
                leftovers.append(f"session {name} survived kill-server")
        # Only AFTER the re-probe: the probe needs the socket the dir holds.
        # Not under tmp_path, so pytest's own cleanup never sees it.
        if self.socket_dir:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        return leftovers


@pytest.fixture
def fleet(tmp_path):
    budget = Budget(_BUDGET_S)
    f = _Fleet(tmp_path, budget)
    try:
        f.wait_ready()
        yield f
    finally:
        leftovers = f.teardown()
    assert not leftovers, f"cleanup left real multiplexer state behind: {leftovers}"


class TestTheFleetIsReadThroughARealMultiplexer:
    def test_sessions_json_reports_live_state_model_and_effort(self, fleet):
        rows = fleet.sessions()

        for name in (fleet.alpha, fleet.beta):
            assert rows[name]["live"] is True, rows[name]
            assert rows[name]["state"] == "idle", rows[name]
            assert rows[name]["model"] == "Fable 5.1", rows[name]
            assert rows[name]["effort"] == "high", rows[name]
        # Configured but never started: no session, and therefore no footer to
        # invent a model from.
        assert rows[fleet.dead]["live"] is False
        assert rows[fleet.dead]["state"] == "dead"
        assert rows[fleet.dead]["model"] is None

    def test_peek_prints_exactly_the_last_lines_of_the_real_pane(self, fleet):
        result = fleet.cli("peek", fleet.alpha, "-n", "3")

        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip("\n").splitlines()
        assert len(lines) == 3, lines
        # The hints row is LAST on a real pane -- the geometry that makes
        # fleet.looks_unsent read the caret line instead of the last one.
        assert "bypass permissions on" in lines[-1]
        assert "high" in lines[-2]  # the footer sits directly above it


class TestASendReachesTheRealPane:
    def test_the_prompt_arrives_verbatim_and_is_acknowledged(self, fleet):
        prompt = "hello from the fleet tier 1234"

        result = fleet.cli("send", fleet.alpha, prompt)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "OK" in result.stdout
        # The wire's own record: exactly once, byte for byte, no shell in between.
        assert fleet.received(fleet.alpha) == [prompt]
        peeked = fleet.cli("peek", fleet.alpha, "-n", "12")
        assert f"ack: {prompt}" in peeked.stdout, peeked.stdout

    def test_an_unknown_name_exits_2_and_lists_the_live_ones(self, fleet):
        result = fleet.cli("send", "nosuch", "x")

        assert result.returncode == 2
        assert fleet.alpha in result.stderr
        assert fleet.beta in result.stderr


class TestSlashCommandsSurviveAShellThatRewritesPaths:
    """The user-facing promise: ``/model``, ``/effort`` and ``/compact`` are
    built in Python and handed to psmux as list argv, so Git Bash / MSYS never
    sees a leading slash to rewrite into ``C:/Program Files/Git/...``."""

    def test_model_and_effort_reach_the_pane_verbatim_through_bash(self, fleet):
        result = fleet.cli_via_bash(
            "model", fleet.alpha, "opus", "--effort", "xhigh", timeout=120.0
        )
        if result is None:
            pytest.skip("no bash on PATH to prove the MSYS-rewrite case")

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "ok" in result.stdout
        received = fleet.received(fleet.alpha)
        assert "/model opus" in received, received
        assert "/effort xhigh" in received, received
        assert not [line for line in received if "Program Files" in line], received

        rows = fleet.sessions()
        assert rows[fleet.alpha]["model"] == "Opus 5", rows[fleet.alpha]
        assert rows[fleet.alpha]["effort"] == "xhigh", rows[fleet.alpha]

    def test_compact_reaches_the_pane_verbatim_through_bash(self, fleet):
        result = fleet.cli_via_bash(
            "send", fleet.beta, "--compact", "post-bash prompt 7777", timeout=120.0
        )
        if result is None:
            pytest.skip("no bash on PATH to prove the MSYS-rewrite case")

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert fleet.received(fleet.beta)[:2] == ["/compact", "post-bash prompt 7777"]

    def test_a_slash_prompt_typed_AS_a_shell_argument_is_the_shells_business(
        self, fleet
    ):
        """...and magent still delivers what it was handed, byte for byte.

        The boundary, pinned rather than wished away: a literal ``/compact``
        typed as a TEXT ARGUMENT in Git Bash is rewritten by the MSYS runtime
        BEFORE magent's process starts (measured: argv arrives as
        ``C:/Program Files/Git/compact``). That is why the product builds its
        slash commands internally and offers ``--compact`` as a flag. What this
        proves about magent is the other half: whatever argv it receives reaches
        the pane unaltered, so magent adds no second layer of corruption.
        """
        result = fleet.cli_via_bash("send", fleet.alpha, "/compact")
        if result is None:
            pytest.skip("no bash on PATH to prove the MSYS-rewrite case")

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        delivered = fleet.received(fleet.alpha)[-1]
        if delivered != "/compact":
            # The ordinary Git-Bash case: the shell's rewrite, delivered intact.
            assert delivered.endswith("/compact"), delivered
            assert "\\" not in delivered, delivered


class TestModelSwitchingAcrossTheWholeLiveFleet:
    def test_model_all_switches_every_live_session(self, fleet):
        result = fleet.cli("model", "--all", "sonnet", timeout=120.0)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        rows = [line for line in result.stdout.splitlines() if " ok" in line]
        assert len(rows) == 2, result.stdout
        live = fleet.sessions()
        for name in (fleet.alpha, fleet.beta):
            assert live[name]["model"] == "Sonnet 4.5", live[name]
            assert "/model sonnet" in fleet.received(name), fleet.received(name)
        # The dead project is not a live session and must not be touched.
        assert fleet.dead not in result.stdout


class TestWaitingOutARealMidTurnAgent:
    def test_wait_idle_waits_for_the_busy_window_to_close(self, fleet):
        assert fleet.cli("send", fleet.beta, f"/busy {_BUSY_S:g}").returncode == 0
        assert _wait_until(
            lambda: fleet.sessions()[fleet.beta]["state"] == "busy", 6.0, 0.3
        ), "a real mid-turn pane never classified as busy"

        started = time.monotonic()
        result = fleet.cli(
            "send", fleet.beta, "--wait-idle", "--timeout", "30", "after busy 5678"
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        # It WAITED rather than raced: the second send could not finish before
        # the busy window did. Slack covers the poll interval plus the time the
        # first CLI process itself spent.
        assert elapsed >= _BUSY_S - 4.0, elapsed
        assert fleet.received(fleet.beta) == [
            f"/busy {_BUSY_S:g}",
            "after busy 5678",
        ]

    def test_compact_runs_first_and_the_prompt_follows_it(self, fleet):
        result = fleet.cli(
            "send", fleet.alpha, "--compact", "post-compact prompt 9999", timeout=120.0
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "idle=True" in result.stdout, result.stdout
        assert fleet.received(fleet.alpha) == [
            "/compact",
            "post-compact prompt 9999",
        ]


class TestAnUnsubmittedPromptIsReportedAsNotConfirmed:
    """Exit 4 against the real geometry.

    RED before the ``looks_unsent`` fix and GREEN after it: the prompt sits on
    the caret line with a rule, the footer and the hints row BELOW it, so the
    old "is it on the pane's last line?" test could never see it.
    """

    def test_a_held_pane_reports_exit_4_and_still_receives_the_prompt(self, fleet):
        prompt = "this text stays unsent 4242"
        assert fleet.cli("send", fleet.beta, f"/hold {_HOLD_S:g}").returncode == 0

        result = fleet.cli("send", fleet.beta, prompt)

        assert result.returncode == 4, f"{result.stdout}\n{result.stderr}"
        assert "peek" in result.stderr, result.stderr

        # While the hold lasts, the prompt really is sitting on the CARET line
        # -- four rows above the bottom, under the rule/footer/hints stack.
        # Guarded rather than asserted flat: on a slow runner the hold may
        # already have elapsed by now, and "the agent finally read it" is the
        # next assertion's business, not a failure of this one.
        if prompt not in fleet.received(fleet.beta):
            lines = fleet.capture(fleet.beta).rstrip().splitlines()
            assert any(
                line.lstrip().startswith(_CARET) and prompt in line for line in lines
            ), lines
            assert prompt not in lines[-1], "the hints row must still be last"

        # ...and "not confirmed" means NOT YET, never lost: once the hold ends
        # the stand-in reads the queued line like any agent would.
        assert _wait_until(
            lambda: prompt in fleet.received(fleet.beta), _HOLD_S + 10.0, 0.4
        ), fleet.received(fleet.beta)
