"""The packaged-install user journey, driven through the INSTALLED ``magent``
console-script entry point (never ``python -m magent`` from the repo).

What each test proves about a real ``pip install magent-multi-ai-agents-manager`` user (no mocks, no
monkeypatching of magent code, no dev/optional deps in the venv):

* the wheel installs a working entry point that answers ``--version`` / ``--help``;
* every runtime module imports on base deps alone -- the dev-dependency-leak
  sweep (a module that quietly ``import``\\s a dev/optional package would fail
  here and nowhere else in the suite);
* a virgin machine (empty home, no config anywhere) gets the documented
  config-missing contract from ``status`` / ``status --json`` and from the
  non-interactive no-subcommand path;
* the launch pipeline itself runs from the wheel under ``--dry-run``;
* the two optional extras degrade gracefully when absent: ``mobile`` prints the
  qrcode install tip, and ``attention`` toast logs the winotify install tip --
  enabled-but-missing must not crash.

Isolation mirrors the tests/e2e / tests/platform tier: child processes get a
redirected home (HOME + the win32 APPDATA / linux XDG config base, so both
``~/.magent`` writes and ``find_config``'s fallback land in tmp), a stripped
``MAGENT_*`` env, and a neutral cwd so ``./src`` can never shadow the
installed package. ``_child_env`` is duplicated per file by convention.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.dist


def _child_env(home: Path) -> dict[str, str]:
    """Real user env with HOME *and* the config base redirected under ``home``,
    and every ``MAGENT_*`` stripped.

    A superset of the tests/e2e helper on purpose: ``env.config_base()`` reads
    ``APPDATA`` on win32 and ``XDG_CONFIG_HOME`` on linux (NOT ``Path.home()``),
    so redirecting only the home vars would let a virgin first-run fall back to
    the developer's real ``%APPDATA%\\magent\\config.json``. The e2e/platform
    tests dodge this by always passing ``--config``; the virgin-first-run tests
    here exercise the fallback, so they must pin the config base too.

    ``PYTHONPATH``/``PYTHONHOME`` are stripped as well: inherited into the
    pristine venv's interpreter they would splice dev paths back into
    ``sys.path`` and silently void the tier's core guarantee.
    """
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
    env["APPDATA"] = home_s  # win32 config_base()
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s  # linux config_base()
    return env


def _wait_until(check, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _run(entry_point: Path, *args: str, home: Path, cwd: Path, **kw):
    return subprocess.run(
        [str(entry_point), *args],
        capture_output=True,
        text=True,
        timeout=kw.pop("timeout", 90),
        cwd=str(cwd),
        env=_child_env(home),
        **kw,
    )


# --- entry point ------------------------------------------------------------


def test_installed_entry_point_reports_version(packaged, home, neutral_cwd):
    r = _run(packaged.entry_point, "--version", home=home, cwd=neutral_cwd)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "1.0.0" in r.stdout, f"version banner missing 1.0.0:\n{r.stdout}"


def test_installed_entry_point_shows_help(packaged, home, neutral_cwd):
    r = _run(packaged.entry_point, "--help", home=home, cwd=neutral_cwd)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "Usage" in r.stdout
    # The group docstring proves the real app object loaded, not a stub.
    assert "Open every project" in r.stdout


# --- dev-dependency leak detector -------------------------------------------

_IMPORT_SWEEP = dedent(
    '''
    """Import every module in the installed magent package on base deps only.

    Run INSIDE the pristine venv. A module that quietly imports a dev/optional
    package (winotify / qrcode / pytest / playwright / ...) fails here.
    (sentry_sdk is a base dep now, so it is present in the pristine venv and
    no longer a leak indicator.) Two sanctioned platform-gated exceptions,
    deliberately asymmetric:

    * magent.hotkey -- documented CONTRACT: MUST raise ImportError off-win32
      and MUST import cleanly on win32. Pinned in both directions below;
      importing cleanly off-win32 would be a regression.
    * magent.platform.windows -- TOLERATED off-win32 only: its top-level
      `from ctypes import WINFUNCTYPE, ...` fails on POSIX ctypes incidentally,
      and only win32's get_platform() ever imports it. NOT a contract -- if it
      later becomes importable everywhere, that is fine, not a failure. On
      win32 the walk still enforces that it imports.

    Everything else stays strict: platform/macos.py and platform/linux.py must
    import on every OS.
    """
    import importlib
    import pkgutil
    import sys

    import magent

    failures = []

    # ImportError tolerated off-win32 (see docstring for the asymmetry).
    _TOLERATED_OFF_WIN32 = {"magent.hotkey", "magent.platform.windows"}


    def _record(name, exc):
        if (
            name in _TOLERATED_OFF_WIN32
            and sys.platform != "win32"
            and isinstance(exc, ImportError)
        ):
            return  # sanctioned platform gating, not a dep leak
        failures.append((name, repr(exc)))


    def _onerror(name):
        _record(name, sys.exc_info()[1])


    for info in pkgutil.walk_packages(
        magent.__path__, magent.__name__ + ".", onerror=_onerror
    ):
        if info.name == "magent.__main__":
            # The `python -m magent` shim runs main() at import (no __name__
            # guard), i.e. the whole CLI -- not a library import. It imports only
            # magent.cli, which this sweep already covers, so skipping it
            # loses no dep-leak coverage while avoiding a spurious SystemExit.
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as exc:  # noqa: BLE001 -- sweep classifies every failure
            _record(info.name, exc)

    # walk_packages does not import leaf modules; pin hotkey's contract directly.
    try:
        importlib.import_module("magent.hotkey")
        hotkey_imported, hotkey_err = True, ""
    except ImportError as exc:
        hotkey_imported, hotkey_err = False, repr(exc)

    if sys.platform == "win32" and not hotkey_imported:
        failures.append(("magent.hotkey", "expected import OK on win32: " + hotkey_err))
    if sys.platform != "win32" and hotkey_imported:
        failures.append(
            ("magent.hotkey", "expected ImportError off-win32, imported cleanly")
        )

    if failures:
        for name, err in failures:
            print("IMPORT-FAIL " + name + " " + err)
        sys.exit(1)
    print("IMPORT-SWEEP-OK")
    '''
)


def test_import_sweep_detects_no_dev_dependency_leakage(
    packaged, home, neutral_cwd, tmp_path
):
    sweep = tmp_path / "sweep.py"
    sweep.write_text(_IMPORT_SWEEP, encoding="utf-8")
    r = subprocess.run(
        [str(packaged.venv_python), str(sweep)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(neutral_cwd),
        env=_child_env(home),
    )
    assert r.returncode == 0, (
        "a runtime module failed to import on base deps alone "
        f"(dev/optional-dep leak):\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert "IMPORT-SWEEP-OK" in r.stdout, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_sentry_sdk_ships_in_the_base_install(packaged, home, neutral_cwd):
    """sentry-sdk is a BASE dependency by design: reporting stays env-gated
    (off until MAGENT_SENTRY_DSN is set), but a plain `pip install
    magent` must always be able to honour a DSN -- the "DSN set, SDK
    missing" doctor warning must be unreachable from a healthy install."""
    r = subprocess.run(
        [str(packaged.venv_python), "-c", "import sentry_sdk"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(neutral_cwd),
        env=_child_env(home),
    )
    assert r.returncode == 0, (
        f"sentry_sdk missing from the pristine base-deps venv:\n{r.stderr}"
    )


# --- virgin first run -------------------------------------------------------


def test_virgin_run_status_reports_missing_config(packaged, home, neutral_cwd):
    """With an empty home and NO config anywhere, `status` honours the
    documented exit-1 config-missing contract -- from the installed artifact."""
    plain = _run(packaged.entry_point, "status", home=home, cwd=neutral_cwd)
    assert plain.returncode == 1, f"stdout:\n{plain.stdout}\nstderr:\n{plain.stderr}"
    assert "No config found" in plain.stderr

    js = _run(packaged.entry_point, "status", "--json", home=home, cwd=neutral_cwd)
    assert js.returncode == 1, f"stdout:\n{js.stdout}\nstderr:\n{js.stderr}"
    # Read .stdout, never a merged stream: the JSON envelope must parse clean.
    assert json.loads(js.stdout) == {"ok": False, "error": "No config found."}


def test_virgin_run_no_subcommand_writes_nothing_and_exits(packaged, home, neutral_cwd):
    """The no-subcommand path on a virgin machine (empty redirected home, no
    config anywhere) exits 1 WITHOUT launching anything or writing a config --
    proven from the installed entry point.

    Two documented branches collapse to the same safe outcome and neither is
    worth pinning an exact message on: cli/app.py runs history *discovery* when
    ``sys.stdin.isatty()`` (it finds nothing in the empty home -> exit 1), and
    prints the ``magent --init`` hint when stdin is a pipe. The interactive
    menu and the discovery prompts are gated on that same isatty check, so they
    cannot be driven deterministically over a plain pipe (a real PTY is not
    portable to Windows) -- so we assert the invariant that holds either way:
    exit 1, no config written under the redirected home, nothing left in cwd.
    """
    r = subprocess.run(
        [str(packaged.entry_point)],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(neutral_cwd),
        env=_child_env(home),
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 1, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    # No config was generated anywhere the redirected config base resolves to...
    assert not (home / "magent" / "config.json").exists()
    assert not (home / ".magent" / "config.json").exists()
    # ...and nothing leaked into the neutral cwd.
    assert list(neutral_cwd.iterdir()) == []
    # It really was the missing-config path (exact message varies by isatty).
    assert "config" in (r.stdout + r.stderr).lower()


def test_dry_run_launch_pipeline_runs_from_installed_wheel(
    packaged, home, neutral_cwd, tmp_path
):
    """`--go --dry-run` drives the real launch pipeline (grid + project select +
    tiling preview) from the installed wheel, without launching anything.

    Gated on real monitors: with none (headless CI without virtual displays)
    run_magent aborts with rc 2 by design, so this asserts the happy path
    only where a display exists (Windows/macOS runners, local dev)."""
    from magent.platform import get_platform  # dev-env probe, for the skip only

    if not get_platform().list_monitors():
        pytest.skip("no real monitors (headless); dry-run aborts rc 2 by design")

    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 1, "rows": 1},
                "settings": {
                    "defaultTool": "probe",
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {"probe": "rem magent-dist-dryrun"},
                },
                "projects": [{"path": str(proj), "title": "distdry"}],
            }
        )
    )
    r = _run(
        packaged.entry_point,
        "--go",
        "--dry-run",
        "--config",
        str(cfg),
        home=home,
        cwd=neutral_cwd,
        timeout=120,
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "DRY RUN" in r.stdout, f"dry-run banner missing:\n{r.stdout}"


# --- optional-extra degradation from the bare env ---------------------------


def test_mobile_without_qrcode_prints_install_tip(packaged, home, neutral_cwd):
    """`magent mobile` in a venv without the optional `qrcode` extra exits 0
    and prints the documented install tip instead of crashing (cli/ui.py)."""
    r = _run(packaged.entry_point, "mobile", home=home, cwd=neutral_cwd)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "pip install qrcode" in r.stdout, f"install tip missing:\n{r.stdout}"


def test_attention_toast_without_winotify_logs_install_tip(
    packaged, home, neutral_cwd, tmp_path
):
    """attention.toast enabled but the optional `winotify` extra absent: the
    ToastRenderer logs one install-tip WARNING and the loop still exits 0.

    OS-agnostic: cli/attention_cmd.py adds the ToastRenderer whenever
    toast=true regardless of platform, and winotify is absent from the bare venv
    on every OS -- so the degraded-not-crashed path is exercised everywhere,
    not just win32. A single pre-seeded needs-input record makes tick 1 a push
    transition, which is what drives the renderer."""
    proj = tmp_path / "proj"
    proj.mkdir()
    title = "disttoast"
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": [{"path": str(proj), "title": title}],
                "settings": {
                    "attention": {
                        "badge": False,
                        "flash": False,
                        "toast": True,
                        "ntfy": False,
                    }
                },
            }
        )
    )
    env = _child_env(home)

    # Seed a needs-input record via the REAL writer, exactly like the hooks do.
    seeded = subprocess.run(
        [
            str(packaged.venv_python),
            "-c",
            "import sys; from magent import agent_state; "
            "agent_state.write_state(sys.argv[1], sys.argv[2])",
            str(proj),
            "needs-input",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(neutral_cwd),
        env=env,
    )
    assert seeded.returncode == 0, f"real state writer failed:\n{seeded.stderr}"
    assert list((home / ".magent" / "state").glob("*.json")), (
        "real writer left no record on disk"
    )

    r = subprocess.run(
        [
            str(packaged.entry_point),
            "--config",
            str(cfg),
            "attention",
            "--ticks",
            "1",
            "--interval",
            "0.5",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(neutral_cwd),
        env=env,
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    log_file = home / ".magent" / "logs" / "attention.log"
    log_text = _wait_until(
        lambda: (
            log_file.read_text(encoding="utf-8", errors="replace")
            if log_file.exists()
            else ""
        ),
        timeout=10,
    )
    assert f"state {title}: new -> needs-input" in log_text, (
        f"the push transition never reached the loop:\n{log_text}"
    )
    assert "attention.toast is on but winotify is not installed" in log_text, (
        f"winotify install-tip WARNING missing from attention.log:\n{log_text}"
    )
