"""Session fixtures for the packaged-install (``dist``) tier.

This tier proves the EXACT environment a real ``pip install magent-multi-ai-agents-manager`` user
gets: a wheel built from *this* source, installed into a PRISTINE venv with NO
extras (only the base deps -- click + pydantic-settings), driven through the
real ``magent`` console-script entry point as a subprocess.

Every other functional tier runs ``python -m magent`` from the dev checkout
with the dev deps present, so none of them can catch a runtime module that
quietly imports a dev/optional package, or a packaging regression (missing
entry point, wheel that omits a submodule). This tier is the one that can.

The ``packaged`` fixture is session-scoped: the wheel is built and installed
exactly once, then shared by every ``dist`` test. Child-process isolation
(redirected home, stripped ``MAGENT_*``, neutral cwd) is re-implemented per
test file as small ``_child_env`` helpers, matching the tests/e2e convention of
light duplication over a shared-helper refactor.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# tests/dist/conftest.py -> tests/dist -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _venv_scripts_dir(venv: Path) -> Path:
    """The venv's executables directory: ``Scripts`` on Windows, ``bin`` else."""
    return venv / ("Scripts" if os.name == "nt" else "bin")


@dataclass(frozen=True)
class Packaged:
    """A magent wheel built from this source and installed into a pristine,
    no-extras venv, plus the paths needed to drive it as a real user would."""

    wheel: Path
    venv_python: Path  # the venv interpreter (import sweep / real state writer)
    entry_point: Path  # the installed ``magent`` console script


@pytest.fixture(scope="session")
def packaged(tmp_path_factory: pytest.TempPathFactory) -> Packaged:
    """Build the wheel once, install it into a fresh no-extras venv, and hand
    back the interpreter + entry-point paths. Any failure fails loudly with the
    subprocess's own stdout/stderr -- a broken build/install IS the diagnosis."""
    exe = ".exe" if os.name == "nt" else ""

    # 1. Build the wheel -- matching CI's `pip install build; python -m build`.
    #    Build from the repo root so hatchling packages src/magent; the
    #    isolated PEP 517 env keeps build artifacts out of the source tree.
    wheelhouse = tmp_path_factory.mktemp("wheelhouse")
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheelhouse),
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, (
        f"wheel build failed:\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )
    wheels = sorted(wheelhouse.glob("magent_multi_ai_agents_manager-*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one magent_multi_ai_agents_manager-*.whl, got {wheels}\n{build.stdout}"
    )
    wheel = wheels[0]

    # 2. Pristine venv -- base deps ONLY (no dev/toast/qr extras; sentry-sdk
    #    rides along as a base dep), so the
    #    install is byte-for-byte what a plain `pip install magent-multi-ai-agents-manager` produces.
    venv_dir = tmp_path_factory.mktemp("pristine-venv")
    made = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert made.returncode == 0, f"venv create failed:\n{made.stderr}"
    venv_python = _venv_scripts_dir(venv_dir) / f"python{exe}"
    assert venv_python.exists(), f"venv python missing at {venv_python}"

    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-input", str(wheel)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert install.returncode == 0, (
        f"wheel install failed:\nstdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )
    entry_point = _venv_scripts_dir(venv_dir) / f"magent{exe}"
    assert entry_point.exists(), f"installed entry point missing at {entry_point}"

    return Packaged(wheel=wheel, venv_python=venv_python, entry_point=entry_point)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty redirected home for one test. Child processes point HOME (and
    the win32 APPDATA / linux XDG config base) here, so ~/.magent writes and
    the config-base lookup both land in tmp, never the developer's real home."""
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def neutral_cwd(tmp_path: Path) -> Path:
    """A cwd outside the repo tree with NO magent.config.json in it, so the
    installed package can never be shadowed by ``./src`` and ``find_config()``
    never picks up a stray cwd config."""
    d = tmp_path / "work"
    d.mkdir()
    return d
