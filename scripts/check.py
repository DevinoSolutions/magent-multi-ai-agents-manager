#!/usr/bin/env python
"""Single source of truth for the magent quality gate.

    uv run python scripts/check.py          # full gate (default)
    uv run python scripts/check.py --fast   # pre-commit: seconds, not minutes

Runs on the current interpreter; CI additionally runs `compileall` across the
full 3.10-3.14 matrix (see ci.yml). Runs every step and exits non-zero if any
step failed.
"""

from __future__ import annotations

import subprocess
import sys

FAST_STEPS: list[tuple[str, list[str]]] = [
    ("ruff  (lint + 3.10 syntax floor)", ["ruff", "check", "src", "tests", "scripts"]),
    (
        "ruff format --check",
        ["ruff", "format", "--check", "src", "tests", "scripts"],
    ),
    (
        "custom lint (MD001-MD005)",
        [sys.executable, "scripts/lint_rules.py"],
    ),
    (
        "ty  (strict type check)",
        [
            "ty",
            "check",
            "src",
            "scripts",
            "--error-on-warning",
            # excluded here; checked by the dedicated win32-platform pass below
            "--exclude",
            "src/magent/platform/windows.py",
            "--exclude",
            "src/magent/hotkey.py",
        ],
    ),
    (
        "ty  (win32 modules, platform view)",
        [
            "ty",
            "check",
            "src/magent/platform/windows.py",
            "src/magent/hotkey.py",
            "--python-platform",
            "win32",
            "--error-on-warning",
        ],
    ),
]

FULL_ONLY_STEPS: list[tuple[str, list[str]]] = [
    (
        "compileall (current interpreter)",
        [sys.executable, "-m", "compileall", "-q", "src"],
    ),
    ("vulture  (dead code)", ["vulture"]),
    (
        "pytest + coverage",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/",
            "--cov=magent",
            "--cov-report=term-missing",
            # 52, not 57: the "Lint + Type Check + Coverage" CI job runs this on
            # ubuntu-latest, where platform/windows.py + hotkey.py are 0%-coverable
            # by construction -- measured there was 53.18%, vs 58.48% on a Windows
            # dev box running the same suite. The floor must hold on the OS that
            # actually enforces it, not on whichever OS happened to set it.
            "--cov-fail-under=52",
        ],
    ),
]


def main() -> int:
    fast = "--fast" in sys.argv[1:]
    steps = FAST_STEPS if fast else FAST_STEPS + FULL_ONLY_STEPS

    failed: list[str] = []
    for name, cmd in steps:
        print(f"\n=== {name} ===", flush=True)
        if subprocess.run(cmd, check=False).returncode != 0:
            failed.append(name)
    if failed:
        print("\nGATE FAILED: " + ", ".join(failed))
        return 1
    mode = "FAST" if fast else "FULL"
    print(f"\nGATE PASSED ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
