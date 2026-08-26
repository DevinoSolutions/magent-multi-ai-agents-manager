"""Type-to-filter picking, driven under a REAL pseudo-terminal.

Sibling of ``test_pty_menu.py`` and the only place the raw-key half of
``cli/picker.py`` runs at all: raw mode is gated on ``sys.stdin.isatty()``, and
Click's ``CliRunner`` fakes a tty well enough for line input but has no console
to read a keystroke from. Everything here therefore goes through a genuine pty
(pexpect on POSIX, pywinpty/ConPTY on Windows) and asserts on the plain
on-screen text once ANSI is stripped -- the highlight marker is a literal
``>``, chosen so it survives that stripping and reads the same to a human.

Two flows, both of which a mocked terminal cannot prove:

* the MENU narrows as you type, marks the best match, and Enter takes THAT row
  rather than the default a bare Enter would have taken;
* the SESSION SWITCHER does the same over real project names, with Down moving
  the highlight and Enter attaching -- proven on disk, by the argv a real
  executable named ``psmux`` recorded when the product spawned it.

The multiplexer is the same recording stand-in the Alt+V tier uses: a real
binary on a tmp PATH, resolved by the product's own ``shutil.which``, so
nothing between the keystroke and the attach is faked. No real psmux is ever
run, and HOME is redirected wholesale, so this tier is safe on a machine with a
live fleet.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from tests.e2e._pty import Budget, Pty

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")

# One wall-clock allowance per test, clamping every stage inside it -- per-stage
# timeouts otherwise sum to a worst case nobody chose, and a blown CI job
# timeout discards every result rather than failing one test.
PICKER_BUDGET_S = 120.0

# Real key presses, as the bytes a terminal actually delivers.
DOWN = "\x1b[B"
UP = "\x1b[A"
ESC = "\x1b"
ENTER = "\r"


def _child_env(home: Path) -> dict[str, str]:
    """A clean child environment: real PATH etc. preserved, every ``MAGENT_*``
    stripped, HOME + config bases redirected into tmp, colour disabled."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("MAGENT_")
        and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
    }
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    # Neither supervisor may run: one installs a SYSTEM-WIDE keyboard hook and
    # the other starts a REAL upload server, and no HOME redirect contains
    # either of them.
    env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
    env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = env.get("TERM", "xterm")
    return env


def _spawn(env: dict[str, str], cwd: Path, *args: str) -> Pty:
    return Pty(
        [sys.executable, "-m", "magent", *args],
        env=env,
        cwd=str(cwd),
        budget=Budget(PICKER_BUDGET_S),
    )


def _config_json(projects: list[tuple[str, Path]]) -> str:
    return json.dumps(
        {
            "version": 3,
            "projects": [
                {"path": str(path), "title": title, "tool": "probe"}
                for title, path in projects
            ],
            "settings": {
                "defaultTool": "probe",
                "tools": {"probe": "rem magent-pty-picker-test"},
                "uploadServer": False,
                "attention": {
                    "badge": False,
                    "flash": False,
                    "toast": False,
                    "ntfy": False,
                },
            },
        }
    )


# The stand-in multiplexer: records every argv it is spawned with, one file per
# invocation (no shared byte range, so nothing can be torn), and always exits 0
# so `has-session -t` reports every configured session live and `attach`
# returns immediately.
_SHIM_BODY = """
import json, os, sys, time
uniq = "%020d-%08d-%s" % (time.time_ns(), os.getpid(), os.urandom(4).hex())
tmp = os.path.join(RECDIR, "." + uniq)
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"t": time.time(), "argv": sys.argv[1:]}))
os.replace(tmp, os.path.join(RECDIR, uniq + ".json"))
sys.exit(0)
"""


def _write_shim(bin_dir: Path, rec_dir: Path) -> None:
    """Put a REAL executable named `psmux` on ``bin_dir``.

    Not a mock inside the product: the picker resolves it with the same
    ``shutil.which`` every install uses and spawns it with the same subprocess
    call, so the recorded argv IS what psmux would have received.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    rec_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "psmux_shim.py"
    script.write_text(f"RECDIR = {str(rec_dir)!r}\n" + _SHIM_BODY, encoding="utf-8")
    if sys.platform == "win32":
        (bin_dir / "psmux.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8"
        )
    else:
        shim = bin_dir / "psmux"
        shim.write_text(
            f'#!/bin/sh\nexec {sys.executable!r} {str(script)!r} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)


def _calls(rec_dir: Path) -> list[list[str]]:
    """Every recorded invocation's argv, in spawn order."""
    return [
        json.loads(p.read_text(encoding="utf-8"))["argv"]
        for p in sorted(rec_dir.glob("*.json"))
    ]


def _await_attach(rec_dir: Path, deadline_s: float) -> list[str] | None:
    """Wait, BOUNDED, for the product to spawn an `attach`; return its argv."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        for argv in _calls(rec_dir):
            if "attach" in argv:
                return argv
        time.sleep(0.1)
    return None


class TestTheMenuFiltersAsYouType:
    def test_typing_narrows_the_menu_and_enter_takes_the_marked_row(self, tmp_path):
        """``qui`` leaves exactly one row standing, marked -- and Enter takes
        it. A bare Enter here would have taken the menu's default (option 1,
        "launch everything"), so a clean exit is itself the proof that Enter
        followed the highlight rather than the default."""
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        project = tmp_path / "solo"
        project.mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(_config_json([("solo", project)]), encoding="utf-8")

        pty = _spawn(_child_env(home), work, "--config", str(cfg))
        try:
            pty.expect("Quit")  # the unfiltered menu painted
            pty.send_keys("qui")
            pty.expect("> q   Quit")  # narrowed to one row, and it is marked
            pty.send_keys(ENTER)
            status = pty.wait_exit()
        finally:
            pty.close()

        assert status == 0, f"Enter did not take the marked row\n{pty.transcript}"

    def test_arrows_move_the_mark_within_the_filtered_rows(self, tmp_path):
        """``st`` matches several rows; Down and Up walk the mark between them.

        The rows it lands on are asserted but never CHOSEN: every action behind
        them would touch this machine's real fleet, so the query is escaped
        away and the run ends on Quit.
        """
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        project = tmp_path / "solo"
        project.mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(_config_json([("solo", project)]), encoding="utf-8")

        pty = _spawn(_child_env(home), work, "--config", str(cfg))
        try:
            pty.expect("Quit")
            pty.send_keys("st")
            # "Status" is the only prefix match, so it is the recommendation.
            pty.expect("> t   Status")
            pty.send_keys(DOWN)
            pty.expect("> a   Attach to a remote host")
            pty.send_keys(UP)
            pty.expect("> t   Status")
            pty.send_keys(ESC)
            # Escape clears the query: the rows it had filtered out are back.
            pty.expect("Edit config")
            pty.send_line("q")
            status = pty.wait_exit()
        finally:
            pty.close()

        assert status == 0, f"menu did not quit cleanly\n{pty.transcript}"


class TestTheSessionSwitcherFiltersTheSameWay:
    def test_typing_a_project_name_filters_and_enter_attaches(self, tmp_path):
        """The headline flow, end to end: open the switcher, type part of a
        project name, watch the list narrow with the best match marked, move
        the mark down one, press Enter -- and find the attach the product
        really spawned recorded on disk, aimed at the row that was marked."""
        home = tmp_path / "home"
        home.mkdir()
        work = tmp_path / "work"
        work.mkdir()
        bin_dir = tmp_path / "bin"
        rec_dir = tmp_path / "rec"
        _write_shim(bin_dir, rec_dir)

        names = ["alpha-api", "beta-web", "gamma-webdocs"]
        projects = []
        for name in names:
            path = tmp_path / "projects" / name
            path.mkdir(parents=True)
            projects.append((name, path))
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(_config_json(projects), encoding="utf-8")

        env = _child_env(home)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        pty = _spawn(env, work, "--config", str(cfg))
        try:
            pty.expect("Open session switcher")
            pty.send_line("s")
            pty.expect("psmux sessions")
            pty.expect("gamma-webdocs")  # the unfiltered list painted
            pty.send_keys("web")
            # "alpha-api" cannot match; both -web rows do, at the same tier,
            # so the ORIGINAL order decides and beta-web is the recommendation.
            pty.expect("> 2   beta-web")
            pty.send_keys(DOWN)
            pty.expect("> 3   gamma-webdocs")
            pty.send_keys(ENTER)
            attach = _await_attach(rec_dir, 30.0)
            # Leave the switcher, then the menu.
            pty.send_line("q")
            pty.send_line("q")
            status = pty.wait_exit()
        finally:
            pty.close()

        assert attach is not None, f"nothing ever attached\n{pty.transcript}"
        assert attach == ["-L", "gamma-webdocs", "attach"], (
            f"attached to the wrong session: {attach}\n{pty.transcript}"
        )
        assert status == 0, f"did not exit cleanly\n{pty.transcript}"
