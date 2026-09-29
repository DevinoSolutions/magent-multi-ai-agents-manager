"""The desktop half of the Session-0 hand-off: start the command, keep the
handle CreateProcess returned, and record the command's exit code.

``run_on_desktop`` copies this file byte for byte into each hand-off's scratch
directory as ``launch.py``, next to an ``argv.json`` it wrote, and the
scheduled task's ``run.ps1`` runs it as ``python -I launch.py``. It is a
Python file and not more PowerShell because PowerShell cannot hold the handle:
Windows PowerShell 5.1's ``Start-Process`` with redirection closes the handle
CreateProcess gave it, so ``$p.Handle`` re-opens the process BY PID, after the
fact -- and a command that has already exited by then has no exit code left to
read. The launcher recorded an empty ``rc.txt`` for a bring-up that worked.
``subprocess.Popen`` keeps the CreateProcess handle, so ``wait()`` reads the
exit code however fast the child was.

Stdlib only, and nothing from magent: this runs as a loose script, outside the
package. ``-I`` keeps PYTHONPATH, PYTHONHOME and the script's own directory
off ``sys.path``, so nothing can put a different module under one of these
imports. It reads no environment variable and sets none: the child inherits
the launcher's environment (``run.ps1`` has exported
``MAGENT_SESSION0_POLICY=refuse`` for it).

The file contract with the poll in ``WindowsPlatform._await_handoff``:
``pid.txt`` once the child exists, ``rc.txt`` last. Each is one decimal
integer and a newline, written to a temporary name and renamed into place, so
a reader never sees a half-written one. A command that cannot be started gets
its reason appended to ``err.txt`` and ``rc.txt`` = 1, with no ``pid.txt``:
the answer comes at once instead of after the start grace.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

SPEC = "argv.json"
LAUNCHER = "launch.py"
OUT = "out.txt"
ERR = "err.txt"
PID = "pid.txt"
RC = "rc.txt"


def write_spec(work: Path, argv: list[str], cwd: str) -> None:
    """Stage what the launcher will run. ``json.dumps`` escapes every non-ASCII
    code point -- a lone surrogate included -- so the file is ASCII and the
    argv round-trips exactly, whatever the code page."""
    (work / SPEC).write_text(json.dumps({"argv": argv, "cwd": cwd}), encoding="ascii")


def read_spec(work: Path) -> tuple[list[str], str]:
    """The staged argv and working directory. ValueError if it is anything
    other than a non-empty list of strings and a string."""
    data: object = json.loads((work / SPEC).read_text(encoding="ascii"))
    if isinstance(data, dict):
        raw, cwd = data.get("argv"), data.get("cwd")
        if isinstance(raw, list) and isinstance(cwd, str):
            argv = [item for item in raw if isinstance(item, str)]
            if argv and len(argv) == len(raw):
                return argv, cwd
    raise ValueError(f"{SPEC} is not a hand-off spec")


def record(path: Path, value: int) -> None:
    """Write ``value`` to ``path`` atomically: the poll reads these files every
    250ms, and must see either no file or the whole number."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(f"{value}\n".encode("ascii"))
    os.replace(tmp, path)


def signed_exit_code(code: int) -> int:
    """An exit code as the signed 32-bit integer Windows tools report.
    ``GetExitCodeProcess`` returns a DWORD, so an NTSTATUS such as
    0xC000013A (a console closed under it) reads as 3221225786; everything
    that prints exit codes on Windows shows it as -1073741510."""
    return code - (1 << 32) if code >= (1 << 31) else code


def spawn(
    argv: list[str], cwd: str, out: BinaryIO, err: BinaryIO
) -> subprocess.Popen[bytes]:
    """Start the command with its streams on the two files and stdin on the
    null device -- nobody is at the desktop to type into it, and an inherited
    stdin would be the task's console. On Windows it gets a console of its
    own, hidden: what ``Start-Process -WindowStyle Hidden`` gave it."""
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO(
            dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=subprocess.SW_HIDE
        )
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
    return subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err
    )


def run(work: Path) -> int:
    """Run the command staged in ``work`` and record its pid and exit code.
    Returns the exit code recorded."""
    try:
        argv, cwd = read_spec(work)
        with (work / OUT).open("wb") as out, (work / ERR).open("wb") as err:
            child = spawn(argv, cwd, out, err)
    except (OSError, ValueError) as exc:
        reason = f"hand-off launcher: could not start the command: {exc}\n"
        with (work / ERR).open("ab") as err:
            err.write(reason.encode("utf-8", errors="backslashreplace"))
        record(work / RC, 1)
        return 1
    record(work / PID, child.pid)
    code = signed_exit_code(child.wait())
    record(work / RC, code)
    return code


def main() -> None:
    run(Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
