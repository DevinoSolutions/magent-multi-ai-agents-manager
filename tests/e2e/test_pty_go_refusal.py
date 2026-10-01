"""`--go` on a REAL terminal refuses a title with no UTF-8 form before any row.

F-SUR-1: the piped-stdout half lives in ``test_launch.py``. This is the console
half, which ``CliRunner`` and a pipe cannot reach: on Windows click writes a
console through its own UTF-16 stream (``_winconsole``), and a lone surrogate
crashed THAT too (``'utf-16-le' codec can't encode ...``), after the first
project's row had printed. POSIX gets a real pty with a strict UTF-8 stdout,
which crashed the same way. The refusal now happens at config load, in our
words, so the terminal shows one ``Error:`` line and no listing.

Isolation matches the other pty tiers: HOME and the config bases redirected
into tmp, every ``MAGENT_*`` stripped and the isolation opt-outs set, and a
dry run.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest

from magent.config import SCHEMA_VERSION
from tests.e2e._pty import Budget, Pty

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")

# One dry-run `--go` that loads nothing: a minute is already generous.
GO_BUDGET_S = 60.0


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
    # The same opt-outs every pty tier sets: no keyboard hook, no real upload
    # server, no priority sweep over the real fleet, no Session-0 hand-off.
    env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
    env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
    env["MAGENT_ATTENTION_SUPERVISOR"] = "0"
    env["MAGENT_PSMUX_BOOST"] = "0"
    env["MAGENT_NODE_SYNC"] = "0"
    env["MAGENT_IDLE_REAP"] = "0"
    env["MAGENT_SESSION0_POLICY"] = "allow"
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = env.get("TERM", "xterm")
    return env


def test_go_on_a_real_terminal_refuses_before_any_row(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "plain").mkdir()
    (tmp_path / "api").mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "baseDir": str(tmp_path),
                "settings": {"uploadServer": False},
                "projects": [
                    {"path": "plain", "title": "plain", "color": "#3b82f6"},
                    {"path": "api", "title": "api\ud83d", "color": "#22c55e"},
                ],
            }
        ),
        encoding="utf-8",
    )
    term = Pty(
        # `--all`: on a terminal `--go` would otherwise open its project checklist.
        [
            sys.executable,
            "-m",
            "magent",
            "--config",
            str(cfg),
            "--go",
            "--all",
            "--dry-run",
        ],
        env=_child_env(home),
        cwd=str(tmp_path),
        budget=Budget(GO_BUDGET_S),
    )
    try:
        term.expect(
            "Error: projects[1].title has text with no UTF-8 form"
            " (UnicodeEncodeError): 'api\\ud83d'"
        )
        assert term.wait_exit() == 1
    finally:
        term.close()
    assert "Traceback" not in term.transcript
    assert "plain" not in term.transcript
