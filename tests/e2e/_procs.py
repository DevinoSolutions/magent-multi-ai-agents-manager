"""Teardown for e2e tiers that start DETACHED magent processes.

A detaching launcher (``attention -d``, ``serve --ensure``) hands its child to
the OS and exits; the test learns the child's pid only from the pid file the
child writes once it is up. When the launcher exits nonzero -- ``attention -d``
did, over a daemon that was merely slow on a loaded desktop -- the assertion
fires before any pid is learned, and a teardown that kills only learned pids
leaves the daemon running (and, if it supervises one, the server it spawned).

The net is the command line. Every tier using this names its config file with a
uuid, and every process it causes carries that path in its argv: the daemon
(``--config <cfg>``) and every server it spawns (``upload_server_argv`` forwards
the same ``--config``). Matching on that uuid-bearing path finds exactly this
test's processes and nothing else on the machine -- never by image name, which
would reach a developer's live fleet.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time


def kill_pid(pid: int | None) -> None:
    """Kill exactly one pid (its tree, on Windows) and tolerate it already being
    gone. Never raises. Only ever called with a pid the test created."""
    if not pid:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def pids_whose_argv_contains(marker: str) -> list[int]:
    """Every live process whose command line contains ``marker``, this one
    excluded. ``marker`` must be unique to the test (a uuid-named path)."""
    if sys.platform == "win32":
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            check=False,
        ).stdout
        try:
            rows = json.loads(out or "[]")
        except ValueError:
            return []
        if isinstance(rows, dict):
            rows = [rows]
        found = [
            (row.get("ProcessId"), row.get("CommandLine") or "")
            for row in rows
            if isinstance(row, dict)
        ]
    else:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        ).stdout
        found = []
        for line in out.splitlines():
            head, _, args = line.strip().partition(" ")
            if head.isdigit():
                found.append((int(head), args))
    return [
        pid
        for pid, argv in found
        if isinstance(pid, int) and pid != os.getpid() and marker in argv
    ]


def kill_everything_carrying(marker: str) -> list[int]:
    """Teardown's last word: kill every process whose argv carries ``marker``.

    Killing a daemon can race a spawn it already had in flight, so this sweeps
    until a pass finds nothing (bounded). Returns every pid it killed, so a
    caller can say what the sweep caught."""
    killed: list[int] = []
    for _ in range(3):
        pids = pids_whose_argv_contains(marker)
        if not pids:
            break
        for pid in pids:
            kill_pid(pid)
        killed += pids
        time.sleep(0.5)
    return killed
