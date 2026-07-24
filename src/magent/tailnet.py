"""Tailnet-resolution leaf: the single owner of every `tailscale` CLI probe.

Before this module, ``['tailscale', 'ip', '-4']`` was re-implemented inline at
five sites (launch, upload_server, cli/mobile, cli/background, cli/doctor) plus a
richer status-json variant in cli/background -- and the copies drifted (P1-01).
Like paths.py / titles.py this is a true leaf: stdlib-only, no dependency on
the cli package or any other magent module, so launch.py and
upload_server.py can import it without cycles (LS-A-001).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

_TIMEOUT_S = 5


@dataclass(frozen=True)
class TailnetProbe:
    """Diagnosis-grade result for `magent doctor`'s three-step probe."""

    on_path: bool  # tailscale binary found on PATH
    responding: bool  # the CLI answered within the timeout
    ip: str | None  # first IPv4, None when logged out / down


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a tailscale CLI command; None when the binary is missing, fails to
    spawn, or does not answer within the timeout."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def ip4() -> str | None:
    """Best-effort first Tailscale IPv4 address, or None if Tailscale isn't
    installed, isn't running, or doesn't answer in time."""
    r = _run(["tailscale", "ip", "-4"])
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    return None


def magicdns_host() -> str | None:
    """This machine's MagicDNS name (trailing dot stripped), or None."""
    r = _run(["tailscale", "status", "--json"])
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        dns = (json.loads(r.stdout).get("Self") or {}).get("DNSName", "")
    except ValueError:
        return None
    if isinstance(dns, str) and dns:
        return dns.rstrip(".")
    return None


def probe() -> TailnetProbe:
    """Typed probe for `magent doctor`: binary on PATH -> CLI responding ->
    IPv4 present. Callers own the user-facing wording of each state."""
    if shutil.which("tailscale") is None:
        return TailnetProbe(on_path=False, responding=False, ip=None)
    r = _run(["tailscale", "ip", "-4"])
    if r is None:
        return TailnetProbe(on_path=True, responding=False, ip=None)
    ip = (
        r.stdout.strip().splitlines()[0]
        if r.returncode == 0 and r.stdout.strip()
        else None
    )
    return TailnetProbe(on_path=True, responding=True, ip=ip)
