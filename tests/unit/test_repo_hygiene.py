"""Repo hygiene: the tracked root-level fileset is a committed allowlist (P5-02).

`.gitignore` hides throwaway artifacts (nul, vscode_debug.txt, audit/, …) but
nothing guards *tracked* files at the repository root. This test asserts that the
set of tracked top-level files is a subset of ``ROOT_ALLOWLIST`` — so a new stray
root file reddens the gate until it is either moved into a subdirectory or
consciously added to the allowlist. The reverse check keeps the allowlist honest
(no entry that no longer exists at the root).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The only files permitted at the repository root. Each entry is a deliberate,
# reviewed presence; adding one is a conscious edit, not a drive-by commit.
ROOT_ALLOWLIST = frozenset(
    {
        ".env.example",
        ".gitattributes",
        ".git-blame-ignore-revs",
        ".gitignore",
        ".gitleaks.toml",
        "CHANGELOG.md",
        "CLAUDE.md",
        "DESIGN.md",
        "LICENSE",
        "magent.config.example.json",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "README.md",
        "RELEASING.md",
        "uv.lock",
    }
)


def _tracked_root_files() -> set[str]:
    """Tracked files at the repo root (top-level entries with no path separator)."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line for line in out.splitlines() if line and "/" not in line}


def test_no_stray_tracked_root_files():
    stray = _tracked_root_files() - ROOT_ALLOWLIST
    assert not stray, (
        f"Tracked root files not in ROOT_ALLOWLIST: {sorted(stray)}. "
        "Move them into a subdirectory, or add them to the allowlist with intent."
    )


def test_allowlist_has_no_dead_entries():
    missing = ROOT_ALLOWLIST - _tracked_root_files()
    assert not missing, (
        f"ROOT_ALLOWLIST lists root files that are no longer tracked: {sorted(missing)}. "
        "Remove the stale entries."
    )
