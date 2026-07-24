"""wt cold-broker pin: the product's FIRST-EVER `wt` launch on a fresh machine
must survive the Windows Terminal broker's cold-start path.

The first `wt.exe` invocation on a machine that has never run Windows Terminal
goes through broker/registration startup (package activation, settings
generation) and is materially slower than every warm launch after it. The
launch pipeline's only budget for that is ``settleSeconds`` (default 3) plus
``tiling.RETRY_SECS_EXACT`` (6s of 1s-interval polling) -- if the cold broker
takes longer than that combined ~9s, tiling prints "not found" and gives up on
the window. This test pins, against a REAL cold runner, that a default-timing
``--go`` launch tiles its window without tiling ever giving up.

Coldness is guaranteed by ORDERING, not by the test (a test cannot verify the
broker had never started): the CI job runs this file as its own pytest
invocation as the FIRST step that can possibly spawn `wt` on the fresh
windows-latest VM -- before the main platform sweep, before the psmux install
-- gated on ``MDTEST_WT_COLD=1`` which only that step sets. Locally the gate
is absent, the test skips, and no claim of coldness is ever made on a warm
dev box. (This is the honest pin: "the first wt launch of this VM's life fits
the default settle+retry budget", not "this test made wt cold".)

Timing knobs are deliberately NOT tuned down: the config omits
``settleSeconds``/``launchDelayMs`` so the child runs the shipped defaults --
exactly what a first-run user's cold launch gets.

Isolation mirrors test_real_launch.py: redirected HOME, ``--config`` in
tmp_path, benign ``rem <uuid>`` tool command, uuid-titled window, cleanup
closes exactly that window and kills exactly the marker-tagged cmd.exe.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = [
    pytest.mark.wt_cold,
    pytest.mark.skipif(
        os.environ.get("MDTEST_WT_COLD") != "1",
        reason="wt cold-broker pin runs only as the dedicated first CI step "
        "(MDTEST_WT_COLD=1); on a dev box wt is warm and the pin would lie",
    ),
    pytest.mark.skipif(
        sys.platform != "win32", reason="Windows Terminal broker is win32-only"
    ),
    pytest.mark.skipif(
        shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
    ),
]

_WM_CLOSE = 0x0010


def _child_env(home) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    return env


def _wait_until(check, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def test_first_ever_wt_launch_survives_cold_broker(tmp_path):
    import ctypes

    from magent.platform import get_platform

    plat = get_platform()
    unique = uuid.uuid4().hex[:10]
    name = f"mdcold{unique}"
    title = f"magent:{name}"
    marker = f"mdcold-marker-{unique}"

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "magent.config.json"
    # settleSeconds / launchDelayMs deliberately omitted: shipped defaults.
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 1, "rows": 1},
                "settings": {
                    "defaultTool": "probe",
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {"probe": f"rem {marker}"},
                },
                "projects": [{"path": str(proj), "title": name}],
            }
        )
    )

    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--go", "--config", str(cfg)],
            capture_output=True,
            text=True,
            timeout=300,
            env=_child_env(home),
        )
        elapsed = time.monotonic() - started
        # Surface the cold-launch timing where reviewers can watch it drift:
        # the job's step summary (always rendered, unlike captured stdout).
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"wt cold-broker pin: cold `--go` took {elapsed:.1f}s\n")

        assert result.returncode == 0, (
            f"cold --go failed rc={result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # THE pin: the launch pipeline's own settle+retry machinery resolved
        # the window during tiling -- it never gave up on the cold window.
        assert "not found" not in result.stdout, (
            "tiling gave up on the cold-broker window (settle+retry budget "
            f"exceeded):\n{result.stdout}"
        )

        # And the window really exists on the desktop afterwards.
        hwnd = _wait_until(lambda: plat.find_window(title), timeout=30)
        assert hwnd, (
            f"cold-launch window {title!r} not on the desktop; magent: windows: "
            f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}"
        )
    finally:
        hwnd = plat.find_window(title)
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        _wait_until(lambda: plat.find_window(title) is None, timeout=15)
        # Belt-and-braces: kill the marker cmd if the window close left it.
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
                f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}",
            ],
            capture_output=True,
            check=False,
        )

    assert plat.find_window(title) is None, f"cleanup left {title!r} on the desktop"
