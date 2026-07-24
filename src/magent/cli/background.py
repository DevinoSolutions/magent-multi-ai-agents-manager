"""Runtime-probe / daemon-bootstrap leaf: port/pid liveness checks and the
detached-process launchers for the upload server and Alt+V listener. Heavy
subsystems (launch, platform, upload_server) and the platform-guarded hotkey
import stay in-body -- hoisting them would make every command module pay to
import launch/platform at `magent --help` time (cli/__init__ imports every
command module eagerly to register it).
"""

from __future__ import annotations

import re
import socket
import sys
import time
from pathlib import Path

from magent import tailnet
from magent.procs import pid_alive


def _maybe_start_upload_server(port: int, config_path: str | None) -> None:
    """Start the upload server detached, unless something is already on the port."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        pass
    else:
        return  # already listening
    finally:
        probe.close()

    args = [sys.executable, "-m", "magent"]
    if config_path:
        args += ["--config", config_path]
    args += ["serve", "-p", str(port)]
    # heavy subsystem: in-body per policy. Must outlive the SSH bring-up
    # command that spawns it -- see spawn_detached.
    from magent.launch import spawn_detached

    spawn_detached(args)


def _maybe_start_hotkey(server_url: str) -> int | None:
    """Start the Alt+V listener hidden in the background, unless one is running.

    The listener's progress now shows in the magent: windows, so it needs no terminal
    of its own -- attach launches it detached and returns, instead of blocking a
    terminal on a message loop. Returns the listener pid (existing or freshly
    started), or None if it couldn't be confirmed.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_hotkey():
        return None

    from magent.hotkey import (
        listener_pid,  # ImportError off-Windows (hotkey.py guards); must stay lazy
    )

    existing = listener_pid()
    if existing:
        return existing

    args = [sys.executable, "-m", "magent", "hotkey", "-s", server_url]
    from magent.launch import spawn_detached  # heavy subsystem: in-body per policy

    spawn_detached(args)
    # The child writes its pid only after the keyboard hook installs; give it a
    # short window to come up so we can report (and so a hook failure surfaces).
    for _ in range(20):
        time.sleep(0.1)
        pid = listener_pid()
        if pid:
            return pid
    return None


def _probe_port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return False
    else:
        return True
    finally:
        s.close()


def _running_upload_port() -> int | None:
    """Port of a *live* locally-running upload server, from its pid file. Skips
    stale pid files (a port whose recorded process is gone)."""
    from magent.upload_server import (
        server_pid,  # heavy subsystem: in-body per policy
    )

    d = Path.home() / ".magent"
    if not d.exists():
        return None
    ports = []
    for f in d.glob("upload_server-*.pid"):
        m = re.match(r"upload_server-(\d+)\.pid", f.name)
        if m:
            ports.append(int(m.group(1)))
    alive = [p for p in ports if pid_alive(server_pid(p))]
    return min(alive) if alive else None


def _tailnet_host() -> str:
    """Best host for the phone URL: Tailscale MagicDNS name, then its IP, then
    the LAN IP. MagicDNS gives the prettiest, most stable URL."""
    host = tailnet.magicdns_host() or tailnet.ip4()
    if host:
        return host
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "localhost"
