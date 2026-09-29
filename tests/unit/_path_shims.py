"""PATH shims for the node-script tests: a program ahead of the real one.

One module, like tests/unit/_fake_ccswap.py, so test_node_recall.py
(install_transcripts.sh's private_dir) and test_remote_mux.py (bring_up.sh's
mkdir_private) fail a folder the same way (G-S10: the two had diverging
copies since D and G each grew their own).
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def path_shim(tmp_path: Path, monkeypatch, program: str, body: str) -> None:
    """A ``program`` ahead of the real one on PATH. ``body`` runs with its
    last argument -- the folder a script asked for -- in $last and the real
    program in $real."""
    real = shutil.which(program)
    assert real is not None
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / program
    shim.write_text(
        f"#!/bin/sh\nreal={shlex.quote(real)}\nfor last; do :; done\n{body}",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")


# Another install makes the folder first -- 0700, as its private_dir would
# -- so this one's mkdir finds it there and fails: the race mkdir -p hid.
LOSES_THE_RACE = (
    '"$real" -m 700 -- "$last" || exit 2\n'
    "echo \"mkdir: cannot create directory '$last': File exists\" >&2\n"
    "exit 1\n"
)


MKDIR_REFUSED = "mkdir: cannot create directory '$last': Permission denied"
CHMOD_REFUSED = "chmod: changing permissions of '$last': Operation not permitted"


def refuses(folder: str, said: str) -> str:
    """A program that fails on ``folder``, saying ``said`` and changing
    nothing there, and is the real one for every other path."""
    return (
        f'case "$last" in {shlex.quote(folder)})\n'
        f'  echo "{said}" >&2\n'
        "  exit 1 ;;\n"
        "esac\n"
        'exec "$real" "$@"\n'
    )
