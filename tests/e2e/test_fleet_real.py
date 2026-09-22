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

# The unit tier's fake ccswap, reused rather than forked: CLAUDE.md forbids a
# second one, and the reason is the single thing a fake ccswap exists to make
# impossible -- resolving the REAL binary, which owns the user's live account
# credentials and whose `list` performs a credential-adoption pass that WRITES
# to its store. It is already a genuine on-disk executable, which is exactly
# what an e2e tier needs, so nothing about it had to change.
from tests.unit._fake_ccswap import (
    MAGENT_READY_SETTINGS,
    account,
    make_fake_ccswap,
)

if TYPE_CHECKING:
    from typing import NoReturn

    from magent.launch import RoutePlan
    from tests.unit._fake_ccswap import FakeCcswap

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


def _write_agent_shim(tmp_path: Path, name: str, log: Path) -> Path:
    """A launcher shim that runs the stand-in agent, logging to ``log``.

    A shim rather than an argv because a pane command is read by a SHELL: psmux
    handed a multi-argument command to pwsh, which turned a path containing ``&``
    into a background job. One shell-quoted argument pointing at a file is the
    only shape that is safe on both multiplexers.
    """
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
            "@echo off\r\n" + subprocess.list2cmdline(argv) + "\r\n", encoding="utf-8"
        )
    else:
        shim.write_text("#!/bin/sh\nexec " + shlex.join(argv) + "\n", encoding="utf-8")
        shim.chmod(0o755)
    return shim


def _records(log: Path) -> list[dict[str, object]]:
    """Every JSON record the stand-in has appended to ``log``, in order.

    Two shapes share the file: one ``startup`` record and one record per line
    read. A reader takes the shape it wants and ignores the other, so either can
    grow without breaking the other's tests.
    """
    if not log.exists():
        return []
    return [
        json.loads(raw)
        for raw in log.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]


def _startup_env(log: Path) -> dict[str, object] | None:
    """The pane agent's own view of its routing environment, or None."""
    for record in _records(log):
        startup = record.get("startup")
        if isinstance(startup, dict):
            return startup
    return None


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
        # ...and routing must never run a real `ccswap`: it is the one
        # feature that shells out to a tool holding the user's real account
        # credentials, and no HOME redirect contains a binary on PATH.
        self.env["MAGENT_ACCOUNT_ROUTING"] = "0"
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
        shim = _write_agent_shim(tmp_path, name, log)

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
        return [
            record["line"] for record in _records(self.logs[name]) if "line" in record
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


class _RoutedFleet:
    """Panes created WITH and WITHOUT an account overlay, nothing else.

    Separate from ``_Fleet`` because it answers a different question and must
    answer it cheaply: no magent CLI, no config, no waiting for a painted
    screen -- just "what environment did the agent at the far end of the psmux
    server actually get?".
    """

    def __init__(
        self, tmp_path: Path, budget: Budget, *, ccswap: FakeCcswap | None = None
    ) -> None:
        self.budget = budget
        self.tmp = tmp_path
        self.binary, self.extra_env, self.socket_dir = _resolve_multiplexer(tmp_path)
        # The fake ccswap the PRODUCT reads when the test is about the whole
        # chain rather than about the psmux boundary alone. None for the legs
        # that hand an overlay in; reached through `ccswap` below, which refuses
        # rather than let a leg plan against "ccswap is not installed".
        self._ccswap = ccswap
        stem = f"mgr-{uuid.uuid4().hex[:8]}"
        self.routed = f"{stem}-r"
        self.plain = f"{stem}-p"
        self.product = f"{stem}-w"
        # ...and three more for the legs whose overlay the PLANNER computes: one
        # routed pane, one whose snapshot was refused (and must therefore come
        # up unrouted), and one created by the real launch path. Short, for the
        # sun_path budget `_resolve_multiplexer` documents.
        self.planned = f"{stem}-n"
        self.refused = f"{stem}-u"
        self.planned_real = f"{stem}-q"
        # Stands in for a ccswap profile directory. Nothing reads inside it: the
        # test asserts on the STRING the pane received, which is the whole
        # contract between magent and the agent.
        self.profile = tmp_path / "profile-13"
        self.profile.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.logs: dict[str, Path] = {}
        self.created: list[str] = []

    @property
    def control_env(self) -> dict[str, str]:
        """Environment for the harness's own psmux CONTROL commands."""
        return {**os.environ, **self.extra_env}

    @property
    def ccswap(self) -> FakeCcswap:
        """The fake ccswap, asserted present.

        A leg that plans without one would have ``find_ccswap()`` answer None,
        the phase would refuse for a reason that has nothing to do with the code
        under test, and the pane would come up unrouted -- green, and proving
        nothing. So absence is a loud failure rather than a silent unrouted pass.
        """
        assert self._ccswap is not None, (
            "this leg plans through the product and needs the fake ccswap; "
            "use the planned_fleet fixture"
        )
        return self._ccswap

    def psmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.control_env,
            check=False,
            timeout=max(5.0, self.budget.clamp(30.0)),
        )

    def _shim(self, name: str) -> Path:
        log = self.tmp / f"{name}.jsonl"
        self.logs[name] = log
        return _write_agent_shim(self.tmp, name, log)

    def create(
        self,
        name: str,
        overlay: dict[str, str] | None,
        *,
        drop: frozenset[str] | None = None,
    ) -> None:
        """Create one pane through the product's OWN environment composition.

        ``psmux.child_env`` is the seam ``platform/windows.py`` passes to its
        ``new-session`` spawn, so the environment block here is the one a routed
        bring-up builds -- byte for byte, including the three strips and the
        conditional credential drop. What is substituted is only the spawn call
        itself, and only because ``launch_psmux_session`` is Windows-only by
        design (``supports_psmux()`` is False on POSIX): the same honest gap this
        tier already carries for the psmux binary.

        ``drop`` defaults to the credential strip that travels with any overlay.
        A caller holding a ``RoutedProject`` passes the PLANNER's own drop set
        instead, so a leg about the routing chain re-derives nothing.
        """
        from magent.env import ACCOUNT_OVERRIDE_VARS
        from magent.psmux import child_env

        if drop is None:
            drop = ACCOUNT_OVERRIDE_VARS if overlay else frozenset()
        env = {**child_env(overlay, drop=drop), **self.extra_env}
        result = subprocess.run(
            [
                self.binary,
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
                str(self.work),
                _pane_command(self._shim(name)),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=max(5.0, self.budget.clamp(30.0)),
        )
        assert result.returncode == 0, (
            f"new-session {name} failed rc={result.returncode}: {result.stderr}"
        )
        self.created.append(name)

    def create_through_the_product(
        self,
        name: str,
        overlay: dict[str, str],
        *,
        drop: frozenset[str] | None = None,
    ) -> None:
        """Create one pane by calling the REAL ``launch_psmux_session``.

        Windows only, because that method is: this is the leg where nothing at
        all is substituted -- the product builds the environment, spawns the
        ``new-session`` client unjobbed, types the command into the pane, and
        the agent at the far end records what it got.

        ``drop`` as in :meth:`create`: defaulted here, supplied from the plan by
        the leg whose overlay the planner computed.
        """
        from magent.env import ACCOUNT_OVERRIDE_VARS
        from magent.platform import PsmuxWindowOpts
        from magent.platform.windows import WindowsPlatform

        shim = self._shim(name)
        self.created.append(name)
        WindowsPlatform().launch_psmux_session(
            [
                PsmuxWindowOpts(
                    window_name=name,
                    cwd=str(self.work),
                    command=f'"{shim}"',
                    env=overlay,
                    drop_env=ACCOUNT_OVERRIDE_VARS if drop is None else drop,
                )
            ]
        )

    # -- the product's own routing decision -----------------------------------

    def plan_through_the_product(self, name: str) -> RoutePlan:
        """The routing phase, run for real against the fake ccswap on disk.

        Nothing here composes an overlay. ``launch._route_projects`` is the whole
        phase the ``--go`` and ``up`` paths both call: the ``--version`` probe,
        one ``config get`` per required setting, the ``list --json`` snapshot,
        ``routing.plan``, and the map write. What comes back is the
        ``RoutedProject`` a real bring-up would put on ``PsmuxWindowOpts`` -- so
        a test that feeds the pane from this answer is feeding it the product's
        arithmetic, not its own.

        The config is built rather than loaded because the FILE is not what this
        leg is about, and ``title`` is the session id routing keys by
        (``launch.routing_session_id`` -> ``psmux.session_name``, which only
        rewrites "." ":" and " "), so the planner's key and the pane's name are
        the same string with no mapping step in between.
        """
        from magent import launch
        from magent.config import (
            AccountSettings,
            MagentConfig,
            ProjectConfig,
            Settings,
        )

        assert self.ccswap.path  # loud now rather than an unrouted pass later
        project_dir = self.tmp / f"proj-{name}"
        project_dir.mkdir(exist_ok=True)
        config = MagentConfig(
            projects=[ProjectConfig(path=str(project_dir), tool="claude", title=name)],
            settings=Settings(
                tools={"claude": "claude"},
                default_tool="claude",
                # The CONFIG gate. The ENV gate is the fixture's business (it is
                # pinned off for every tier), and both have to say yes.
                accounts=AccountSettings(enabled=True),
            ),
        )
        return launch._route_projects(config, config.projects)

    def ccswap_verbs(self) -> list[str]:
        """The ccswap command of every invocation, spelled as the unit tier
        spells it -- the witness that a real subprocess produced the snapshot."""
        return [" ".join(call[:2]) for call in self.ccswap.calls()]

    def startup(self, name: str) -> dict[str, object]:
        """The agent's own startup record, waited for. Fails loudly without it."""
        got = _wait_until(
            lambda: _startup_env(self.logs[name]), self.budget.clamp(_READY_S)
        )
        assert got is not None, (
            f"{name}'s stand-in agent never recorded its environment. "
            f"pane:\n{self.psmux('-L', name, 'capture-pane', '-p', '-t', name).stdout!r}"
        )
        return got

    def teardown(self) -> list[str]:
        leftovers = []
        for name in self.created:
            self.psmux("-L", name, "kill-server")
        for name in self.created:
            live = _wait_until(
                lambda n=name: (
                    self.psmux("-L", n, "has-session", "-t", n).returncode != 0
                ),
                10.0,
            )
            if not live:
                leftovers.append(f"session {name} survived kill-server")
        if self.socket_dir:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        return leftovers


@pytest.fixture
def routed_fleet(tmp_path, monkeypatch):
    # An ambient credential, set the way a user's shell would. A routed pane must
    # not see it (it silently outranks the account the overlay chose, and bills
    # the API instead of the subscription); an UNROUTED pane must still see it,
    # because it is the user's own configuration.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    fleet = _RoutedFleet(tmp_path, Budget(_BUDGET_S))
    try:
        yield fleet
    finally:
        leftovers = fleet.teardown()
    assert not leftovers, f"cleanup left real multiplexer state behind: {leftovers}"


@pytest.fixture
def planned_fleet(tmp_path, monkeypatch):
    """``routed_fleet`` plus a real fake ccswap the PRODUCT reads.

    Everything the routing phase needs to say YES, and nothing more, because a
    leg that routes nothing proves nothing:

    * the ENV gate, put back. ``tests/conftest.py`` pins
      ``MAGENT_ACCOUNT_ROUTING=0`` for every tier -- this module's legs are about
      what happens after a user opts in, the same move the upload supervisor's
      tests make. ``_cached_env`` goes with it, because ``get_env()`` memoises
      and a read that already happened would answer 0 forever.
    * the fake installed through the ``find_ccswap`` SEAM, never PATH.
      ``tests/conftest.py::_no_real_ccswap`` has already made that attribute
      answer "not installed"; overriding the same attribute is the documented way
      to win over it without a ``ccswap`` ever being resolvable from PATH.
    * settings that clear the three ``REQUIRED_SETTINGS`` gates -- the fake's
      DEFAULTS are deliberately the ones magent cannot route under, so a test
      that wants routing has to say so.
    * one eligible, hydrated subscription account whose profile dir is a TMP
      directory. Never the real ``~/.claude-swap-backup``: that is the user's
      live credential store, magent never reads inside it, and conftest's guard A
      watches it.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("MAGENT_ACCOUNT_ROUTING", "1")
    monkeypatch.setattr("magent.env._cached_env", None)
    fake = make_fake_ccswap(tmp_path)
    fake.set_settings(MAGENT_READY_SETTINGS)
    fleet = _RoutedFleet(tmp_path, Budget(_BUDGET_S), ccswap=fake)
    fake.set_accounts([account("13", profile_path=str(fleet.profile))])
    monkeypatch.setattr("magent.accounts.find_ccswap", lambda: fake.path)
    try:
        yield fleet
    finally:
        leftovers = fleet.teardown()
    assert not leftovers, f"cleanup left real multiplexer state behind: {leftovers}"


class TestTheAccountCrossesThePsmuxServerBoundary:
    """R-1, measured rather than assumed -- this tier's whole reason to exist.

    A routed pane's account is an environment variable set exactly once, on the
    ``new-session`` CLIENT. The psmux SERVER that hosts the agent is a grandchild
    that client forks, and Windows does not inherit a priority class across that
    boundary (the reason ``psmux.boost_priority`` is a sweep and not a spawn
    flag). If the environment is dropped there too, every routed pane silently
    runs on the default login -- the worst failure this feature has, because
    nothing on screen would say so.

    The witness is the agent's own JSON log, not a screen scrape: it is the only
    thing that can report what the process at the far end actually received.
    """

    def test_a_routed_pane_reports_its_accounts_config_dir_verbatim(self, routed_fleet):
        routed_fleet.create(
            routed_fleet.routed, {"CLAUDE_CONFIG_DIR": str(routed_fleet.profile)}
        )

        startup = routed_fleet.startup(routed_fleet.routed)

        # Verbatim, not a prefix: the agent writes its transcripts under exactly
        # this path, and magent's session probe reads exactly this path.
        assert startup["CLAUDE_CONFIG_DIR"] == str(routed_fleet.profile)
        # ...and the ambient credential that would have outranked it is gone.
        assert startup["anthropic_api_key_set"] is False

    def test_an_unrouted_pane_sees_no_account_and_keeps_the_users_own_key(
        self, routed_fleet
    ):
        routed_fleet.create(routed_fleet.plain, None)

        startup = routed_fleet.startup(routed_fleet.plain)

        assert startup["CLAUDE_CONFIG_DIR"] is None
        # The strip travels with the overlay and never on its own: stripping a
        # user's own ANTHROPIC_API_KEY out of an unrouted pane would log them
        # out of a feature they never enabled.
        assert startup["anthropic_api_key_set"] is True

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="launch_psmux_session is Windows-only by design (supports_psmux)",
    )
    def test_the_real_launch_path_delivers_the_overlay_into_the_pane(
        self, routed_fleet
    ):
        # The leg with nothing substituted: the product builds the environment,
        # spawns the client, types the command, and the agent reports back.
        routed_fleet.create_through_the_product(
            routed_fleet.product, {"CLAUDE_CONFIG_DIR": str(routed_fleet.profile)}
        )

        startup = routed_fleet.startup(routed_fleet.product)

        assert startup["CLAUDE_CONFIG_DIR"] == str(routed_fleet.profile)
        assert startup["anthropic_api_key_set"] is False


class TestTheAccountTheProductChoseIsTheAccountThePaneRunsOn:
    """The two halves of R-1, joined -- the gap the class above leaves open.

    ``TestTheAccountCrossesThePsmuxServerBoundary`` hands
    ``launch_psmux_session`` an overlay dict the TEST wrote, so it proves the
    second half (an environment set on the ``new-session`` client survives the
    fork to the server that hosts the agent) and says nothing about the first.
    The first half -- ccswap's JSON -> ``routing.plan`` -> ``accounts.profile_env``
    -> ``RoutedProject.env`` -- was pinned only in ``tests/unit/
    test_launch_routing.py``, where the psmux boundary does not exist. Nothing
    proved the two joined, and a feature whose halves are each proven separately
    can still be broken at the seam between them.

    So here nothing in the middle is hand-fed. A real ``ccswap`` subprocess
    prints a snapshot, ``launch._route_projects`` turns it into a route, and the
    pane is created carrying exactly ``route.env``/``route.drop_env`` -- the same
    expression ``launch._dispatch_cli_agent_project`` writes onto
    ``PsmuxWindowOpts``. The witness stays the agent's own JSON log, because it
    is the only thing that can report what the process at the far end received.

    Every assertion about a routed pane is paired with the ROW that routed it,
    which is the guard that matters: routing can never be the reason a bring-up
    fails, so a leg whose five refusals were not all satisfied would come up
    unrouted and pass vacuously. The refusal leg below is the other side of the
    same guard -- it proves an unrouted pane looks DIFFERENT.
    """

    def test_the_overlay_the_planner_computed_reaches_the_pane(self, planned_fleet):
        from magent import accounts

        plan = planned_fleet.plan_through_the_product(planned_fleet.planned)

        # Routed, and provably so BEFORE the pane exists. Every one of the five
        # refusals (old ccswap, a required setting not in effect, a duplicate
        # warning, a snapshot error, no eligible account) leaves the row
        # `unrouted-*` and names itself in a note -- so an empty note tuple plus
        # a route is the whole gate, read from the product's own answer.
        assert plan.notes == (), plan.notes
        route = plan.route(planned_fleet.planned)
        assert route is not None, (
            "the planner routed nothing, so this leg would have proven only that "
            "an unrouted pane comes up -- check the fake's snapshot"
        )
        assert route.account == "13"
        assert route.env == {"CLAUDE_CONFIG_DIR": str(planned_fleet.profile)}
        # ...and it was computed from a real subprocess's output, not a stub.
        assert "list --json" in planned_fleet.ccswap_verbs()

        # The pane is fed the plan and nothing else -- both halves of the
        # environment, overlay AND credential strip, off the RoutedProject that
        # `_dispatch_cli_agent_project` would hand to PsmuxWindowOpts.
        planned_fleet.create(planned_fleet.planned, route.env, drop=route.drop_env)
        startup = planned_fleet.startup(planned_fleet.planned)

        # The chain's far end: the path ccswap reported, through the planner,
        # through `child_env`, through the psmux client -> server fork, as the
        # agent process itself sees it. Verbatim, not a prefix -- transcripts are
        # written under exactly this directory and the session probe reads it.
        assert startup["CLAUDE_CONFIG_DIR"] == route.env["CLAUDE_CONFIG_DIR"]
        # ...and the ambient credential that would have silently outranked the
        # account the planner chose (billing the API instead of the
        # subscription) is gone, because the strip rode along with the overlay.
        assert startup["anthropic_api_key_set"] is False

        # The product's own record of the decision agrees with the pane: same
        # session, same account. A map that named a different one would mean
        # every status surface describing this pane is wrong about it.
        assert accounts.read_map()[planned_fleet.planned].account == route.account

    def test_a_refused_snapshot_starts_the_pane_unrouted(self, planned_fleet):
        """The negative control, and a product law in its own right: routing can
        never be the reason a bring-up fails.

        Without this the leg above could be green for the wrong reason -- a pane
        that shows no ``CLAUDE_CONFIG_DIR`` is indistinguishable from a pane
        whose overlay never arrived unless something proves the two states look
        different. One refusal is enough to prove it (the version gate is the
        cheapest and the most specific), and the same fake, fixture and pane
        machinery answer it.
        """
        planned_fleet.ccswap.set_version("ccswap 0.30.9")

        plan = planned_fleet.plan_through_the_product(planned_fleet.refused)

        route = plan.route(planned_fleet.refused)
        assert route is None
        assert "0.30.9" in "\n".join(plan.notes), plan.notes

        # `route.env if route else None` / `route.drop_env if route else
        # frozenset()` is verbatim what `_dispatch_cli_agent_project` writes onto
        # PsmuxWindowOpts, so this pane is created exactly as the product would
        # create it for a project the phase refused to route.
        planned_fleet.create(
            planned_fleet.refused,
            route.env if route else None,
            drop=route.drop_env if route else frozenset(),
        )
        startup = planned_fleet.startup(planned_fleet.refused)

        assert startup["CLAUDE_CONFIG_DIR"] is None
        # The user's own key survives: the strip travels with an overlay and
        # never on its own, so a refusal must not log them out of a feature they
        # never enabled.
        assert startup["anthropic_api_key_set"] is True

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="launch_psmux_session is Windows-only by design (supports_psmux)",
    )
    def test_the_real_launch_path_delivers_the_planners_overlay(self, planned_fleet):
        """The whole chain with nothing substituted at all.

        Same honest gap as the class above, in the same direction: only Windows
        has a ``launch_psmux_session`` to call (``supports_psmux()`` is False on
        POSIX), so the cross-OS legs stop one call short of it and this one goes
        the rest of the way -- the product composes the environment, spawns the
        ``new-session`` client unjobbed, types the command into the pane, and the
        agent reports what it inherited.
        """
        plan = planned_fleet.plan_through_the_product(planned_fleet.planned_real)
        route = plan.route(planned_fleet.planned_real)
        assert route is not None, plan.notes

        planned_fleet.create_through_the_product(
            planned_fleet.planned_real, route.env, drop=route.drop_env
        )
        startup = planned_fleet.startup(planned_fleet.planned_real)

        assert startup["CLAUDE_CONFIG_DIR"] == str(planned_fleet.profile)
        assert startup["anthropic_api_key_set"] is False


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
