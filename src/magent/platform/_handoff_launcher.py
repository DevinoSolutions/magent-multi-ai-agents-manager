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
a reader never sees a half-written one. A ``pid.txt`` that cannot be written
is skipped, never fatal: the exit code is what the caller is waiting for. A
command that cannot be started gets its reason appended to ``err.txt`` and
``rc.txt`` = 1, with no ``pid.txt``: the answer comes at once instead of after
the start grace.

One more job: the launcher puts ITSELF back at Normal CPU and I/O priority
before it starts anything. A task registered without an explicit priority gets
Task Scheduler's default of 7, which runs it BelowNormal and at Low I/O -- and
a child inherits both, so a hand-off's psmux server, its pane shells and its
agents all did (measured: a ``claude --continue`` reading a 424 MB transcript
at 0.4 MB/s, 46 MB/s once the same process was set to Normal/2). The fleet is
interactive work; it must not inherit a background task's class.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

SPEC = "argv.json"
LAUNCHER = "launch.py"
OUT = "out.txt"
ERR = "err.txt"
PID = "pid.txt"
RC = "rc.txt"

# How often, and how far apart, a record whose file is held gets retried:
# 0.4s in all, well inside the poll's graces.
_RECORD_TRIES = 5
_RECORD_RETRY_S = 0.1


# NORMAL_PRIORITY_CLASS, and the ProcessIoPriority information class with
# IoPriorityNormal (0 = very low, 1 = low, 2 = normal; 3 = high needs a
# privilege and is never asked for).
_NORMAL_PRIORITY_CLASS = 0x00000020
_PROCESS_IO_PRIORITY = 33
_IO_PRIORITY_NORMAL = 2


def normalize_priority() -> None:
    """Make this process -- and so everything it starts -- Normal CPU and Normal
    I/O priority. Raising Low to Normal needs no privilege. Best effort and
    silent: a refusal leaves the inherited priority, which is slow, not wrong,
    and must never cost the command it was about to run. No-op off Windows."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    with contextlib.suppress(OSError, AttributeError):
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, _NORMAL_PRIORITY_CLASS)
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtSetInformationProcess.argtypes = [
            wintypes.HANDLE,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        level = wintypes.ULONG(_IO_PRIORITY_NORMAL)
        ntdll.NtSetInformationProcess(
            handle, _PROCESS_IO_PRIORITY, ctypes.byref(level), ctypes.sizeof(level)
        )


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
    250ms, and must see either no file or the whole number.

    A PermissionError is retried a few times, briefly: an antivirus scanner or
    the search indexer can hold a file it has just seen being written, and the
    rename then fails for a moment. Anything else, or a file still held after
    the last try, raises."""
    tmp = path.with_name(path.name + ".tmp")
    for attempt in range(_RECORD_TRIES):
        try:
            tmp.write_bytes(f"{value}\n".encode("ascii"))
            os.replace(tmp, path)
        except PermissionError:
            if attempt == _RECORD_TRIES - 1:
                raise
            time.sleep(_RECORD_RETRY_S)
        else:
            return


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
    normalize_priority()
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
    # The pid is only the poll's "it started" signal, and failing to record it
    # must not cost the exit code. The task stays Running while we wait, so
    # the poll's start check waits too, and rc.txt still answers.
    with contextlib.suppress(OSError):
        record(work / PID, child.pid)
    code = signed_exit_code(child.wait())
    record(work / RC, code)
    return code


def main() -> None:
    run(Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
