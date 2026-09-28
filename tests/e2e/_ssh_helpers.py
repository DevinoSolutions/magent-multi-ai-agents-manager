"""The small pieces the ssh-driven e2e tiers share.

Not a test module (no ``test_`` prefix, so pytest never collects it). The real
ssh tier (``test_ssh_real.py``) and the nodes tier (``test_nodes_real.py``)
both need an unroutable address, a free loopback port, a CI annotation that
reaches the log's parser, and an out-of-band kill of one ssh client. They live
here so neither tier imports the other's privates: a rename in one test module
must not break the other tier.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# TEST-NET-1 (RFC 5737): guaranteed never routed, so a dial at it always fails
# in connect() and the local ssh CLIENT emits the real exit 255. That is the
# faithful reproduction of the reported bug -- a laptop that slept, a wi-fi
# change, a host that rebooted all surface as a client-side 255, generated
# here rather than reported by a server. Deliberately NOT simulated with a
# remote command that exits 255: see the Windows note on the sshd legs in
# test_ssh_real.py.
UNROUTABLE = "192.0.2.1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def real_stdout(capsys: pytest.CaptureFixture[str], line: str) -> None:
    """Write to the real step stdout with pytest capture suspended, so GitHub
    ``::warning`` annotations reach the CI log's parser."""
    with capsys.disabled():
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def emit_ci_warning(
    capsys: pytest.CaptureFixture[str], title: str, message: str
) -> None:
    real_stdout(capsys, f"::warning title={title}::{message}")


def kill_ssh_carrying(token: str, *, timeout: float = 30.0) -> None:
    """Kill the ssh CLIENT whose command line carries ``token``, and only it.

    Out-of-band on purpose: the drop has to look to the supervisor exactly like
    a wi-fi failure -- something outside the process killing the connection --
    rather than a remote command choosing to exit. Narrowed to processes
    actually named ``ssh`` so the supervisor itself, whose argv carries the same
    token in ``--remote``, is never the one that dies.

    ``timeout`` bounds each child this runs (POSIX: the ``pgrep``, then one
    ``ps`` per match); the one PowerShell call gets twice that, for its cold
    start.
    """
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{token}*' }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
                ),
            ],
            capture_output=True,
            timeout=2 * timeout,
            check=False,
        )
        return
    found = subprocess.run(
        ["pgrep", "-f", token],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    for pid in found.stdout.split():
        comm = subprocess.run(
            ["ps", "-o", "comm=", "-p", pid],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if os.path.basename(comm.stdout.strip()) == "ssh":
            with suppress(OSError, ValueError):
                os.kill(int(pid), 9)
