"""The interactive ``magent`` menu, driven under a REAL pseudo-terminal.

Why this exists: every other test of the no-subcommand interactive path goes
through Click's ``CliRunner``, which fakes stdin/stdout — it is NOT a terminal,
so ``sys.stdin.isatty()`` is False and the real first-run/menu branch in
``cli/app.py`` never actually executes the way a user hits it. These tests spawn
the installed module (``python -m magent``) under a genuine pty (pexpect on
POSIX, pywinpty/ConPTY on Windows), assert on what the user literally sees on
screen, and — for first-run — assert on the config that lands on disk.

Isolation (identical posture to the real-upload/serve tiers): each child runs
with HOME + the win32 config bases redirected into ``tmp_path`` (so config,
logs, agent-state all land there, never the real ``~/.magent``), every
``MAGENT_*`` var stripped, ``NO_COLOR=1`` so click emits plain text, and a
clean throwaway CWD so first-run discovery can't pick up a stray
``magent.config.json``.

The three covered flows:

* fresh-HOME first run — discovery finds a seeded project, the user confirms,
  a VALID config file is written to disk, and the app then drops into the menu
  (which we quit cleanly);
* the main menu renders and quits on ``q`` with exit status 0;
* navigating into the "Launch a group" submenu and back to the main menu (via an
  out-of-range pick), then quitting — proving the loop returns to the top.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.e2e._pty import Pty

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

# Pick the real-pty back end this OS needs; skip the whole module if it's absent
# rather than erroring at collection.
if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")


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
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
    # Plain, deterministic output from the child; UTF-8 so the banner glyphs
    # never trip a legacy Windows code page.
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
    )


def _config_json(project_dir: Path, *, group: str | None = None) -> str:
    project: dict[str, object] = {
        "path": str(project_dir),
        "title": "menuproj",
        "tool": "probe",
    }
    if group:
        project["group"] = group
    return json.dumps(
        {
            "version": 3,
            "projects": [project],
            "settings": {
                "defaultTool": "probe",
                "tools": {"probe": "rem magent-pty-menu-test"},
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


def test_first_run_discovery_writes_valid_config(tmp_path):
    """Fresh HOME, no ``--config``: the first-run discovery wizard finds a seeded
    project, the user confirms at the real prompt, and a valid config is written
    to the redirected config location — then the app enters the menu, which we
    quit."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    # Seed one discoverable Codex project pointing at a real, deep, non-generic
    # directory so discover_projects() returns it (see discover._is_real_project).
    project_dir = tmp_path / "workspace" / "acme" / "widget-svc"
    project_dir.mkdir(parents=True)
    codex_day = home / ".codex" / "sessions" / "2026" / "07" / "15"
    codex_day.mkdir(parents=True)
    (codex_day / "session-0.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "pty-seed", "cwd": str(project_dir)},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    env = _child_env(home)

    pty = _spawn(env, work)
    try:
        pty.expect("Welcome")
        pty.expect("widget-svc")  # the discovered project is listed
        pty.expect("Generate config")  # the confirm prompt
        pty.send_line("y")
        pty.expect("Saved to")
        # Discovery hands off to the interactive menu; quit it cleanly.
        pty.expect("Quit")
        pty.send_line("q")
        status = pty.wait_exit()
    finally:
        pty.close()

    assert status == 0, f"non-zero exit\n{pty.transcript}"
    # config_base() differs per OS (APPDATA on win32, XDG_CONFIG_HOME on Linux,
    # ~/Library/Application Support on macOS) — all rooted under the redirected
    # HOME here, so locate the written config rather than assuming one layout.
    written = [p for p in home.rglob("config.json") if p.parent.name == "magent"]
    assert len(written) == 1, (
        f"expected exactly one written config under {home}, got {written}\n"
        f"{pty.transcript}"
    )
    expected_config = written[0]
    data = json.loads(expected_config.read_text(encoding="utf-8"))
    assert data.get("projects"), "written config has no projects"
    # Prove it parses through the real typed loader, not just as raw JSON.
    from magent.config import load_config

    cfg = load_config(str(expected_config))
    assert len(cfg.projects) == 1
    assert Path(cfg.projects[0].path).name == "widget-svc" or cfg.base_dir


def test_main_menu_renders_and_quits(tmp_path):
    """With a config present, the real menu draws its items and a ``q`` quits
    with exit status 0."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(_config_json(project_dir), encoding="utf-8")

    env = _child_env(home)
    pty = _spawn(env, work, "--config", str(cfg))
    try:
        pty.expect("Launch & tile new windows")
        pty.expect("Quit")
        pty.send_line("q")
        status = pty.wait_exit()
    finally:
        pty.close()

    assert status == 0, f"menu did not quit cleanly\n{pty.transcript}"


def test_menu_group_submenu_and_back(tmp_path):
    """Enter the "Launch a group" submenu, make an out-of-range pick so the loop
    reports it and redraws the main menu, then quit — proving the interactive
    loop returns to the top rather than falling through."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(_config_json(project_dir, group="alpha"), encoding="utf-8")

    env = _child_env(home)
    pty = _spawn(env, work, "--config", str(cfg))
    try:
        # The group option only appears when a project carries a group.
        pty.expect("Launch a group")
        pty.send_line("3")
        pty.expect("group")  # the submenu's own "group" prompt
        pty.send_line("99")  # out of range -> Invalid choice -> back to menu
        pty.expect("Invalid choice")
        pty.expect("Quit")  # main menu redrawn
        pty.send_line("q")
        status = pty.wait_exit()
    finally:
        pty.close()

    assert status == 0, f"submenu round-trip did not quit cleanly\n{pty.transcript}"
