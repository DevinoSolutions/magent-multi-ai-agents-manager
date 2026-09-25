"""Throwaway real git repositories for the node tests.

The FIXTURE writes (init, commit, push into a bare origin in tmp_path); the
product code under test only ever reads. Every git call carries its own
identity and turns signing off, so neither a developer's nor a runner's
config can make a commit prompt, sign or fail.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

GIT_ID = [
    "-c",
    "user.name=magent test",
    "-c",
    "user.email=test@magent.invalid",
    "-c",
    "init.defaultBranch=main",
    "-c",
    "commit.gpgsign=false",
]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_ID, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def commit(
    repo: Path, name: str = "README.md", text: str = "hello\n", message: str = "init"
) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "--no-verify", "-m", message)


def make_origin_and_clone(tmp_path: Path, name: str = "api") -> tuple[Path, Path]:
    """A bare ``<name>-origin.git`` and a clone of it at ``<name>`` holding one
    pushed commit on ``main``: a clean, fully pushed working tree."""
    origin = tmp_path / f"{name}-origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    clone = tmp_path / name
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    commit(clone)
    git(clone, "push", "-q", "-u", "origin", "main")
    return origin, clone
