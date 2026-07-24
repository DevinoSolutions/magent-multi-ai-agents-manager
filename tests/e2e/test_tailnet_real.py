"""REAL tailnet tier: every test here talks to a live Tailscale node.

``src/magent/tailnet.py`` (``ip4`` / ``magicdns_host`` / ``probe`` -- the
single owner of every ``tailscale`` CLI probe) and the Tailscale-facing half of
``magent serve``'s default bind (``upload_server._bind_addresses``) had only
ever been unit-mocked: no test had ever run the real ``tailscale`` binary or
proven the server actually listens on the machine's Tailscale IPv4. CI joins an
ephemeral, tagged Tailscale node (``tailscale/github-action`` with OAuth) and
exports ``MDTEST_TAILSCALE=1`` for the dedicated ``tailnet`` job. These tests
use it. No fakes, no monkeypatched ``subprocess``:

* ``test_ip4_agrees_with_real_tailscale`` -- ``tailnet.ip4()`` returns exactly
  the first line of real ``tailscale ip -4``, and it is a genuine CGNAT
  (``100.64.0.0/10``) Tailscale address.
* ``test_probe_reports_live_node`` -- ``tailnet.probe()`` reports the binary on
  PATH, the CLI responding, and the same IPv4, live.
* ``test_magicdns_host_matches_status_json`` -- ``tailnet.magicdns_host()``
  equals the real ``tailscale status --json`` ``Self.DNSName`` with its trailing
  dot stripped (a non-empty MagicDNS name).
* ``test_default_bind_list_includes_tailscale_never_wildcard`` -- fed the REAL
  ``tailnet.ip4()``, ``upload_server._bind_addresses(None)`` is exactly
  ``["127.0.0.1", <tailscale ip>]`` and never the ``0.0.0.0`` wildcard.
* ``test_serve_default_bind_answers_on_tailscale_ip`` -- a real ``magent
  serve`` (no ``--host``, so the loopback+Tailscale default) answers ``/health``
  on BOTH ``127.0.0.1`` and the Tailscale IP, and -- proven against the live
  process -- did NOT grab the LAN wildcard: a fresh socket can still bind the
  runner's own LAN address on the same port.

The no-token loopback+tailnet bind IS the access control by design (a hard
boundary; DESIGN.md) -- these tests exercise it as-is and add no auth. Locally
``MDTEST_TAILSCALE`` is simply absent and everything here skips: NEVER install
or join Tailscale on a developer machine for this file. The CI node is
ephemeral and tag-scoped; it evaporates when the job ends.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.needs_tailscale]

# Tailscale hands out node addresses from the 100.64.0.0/10 CGNAT block.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# ---------------------------------------------------------------------------
# Shared gate and helpers
# ---------------------------------------------------------------------------


def _real_tailscale_ip() -> str | None:
    """First IPv4 from real ``tailscale ip -4``, or None when down/absent."""
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    return None


@pytest.fixture
def tailnet_wire() -> str:
    """The CI-provisioned live Tailscale node, or a clean skip when absent."""
    if os.environ.get("MDTEST_TAILSCALE") != "1":
        pytest.skip(
            "live Tailscale node not configured (the `tailnet` CI job joins an "
            "ephemeral tagged node and exports MDTEST_TAILSCALE=1; local runs skip)"
        )
    if shutil.which("tailscale") is None:
        pytest.skip("tailscale binary not on PATH")
    ip = _real_tailscale_ip()
    if not ip:
        pytest.skip("`tailscale ip -4` returned no address (node logged out/down)")
    return ip


def _wait_until(check, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_ip() -> str | None:
    """The runner's own primary (default-route) IPv4 -- the LAN address that a
    ``0.0.0.0`` bind would cover but a loopback+Tailscale bind leaves free.

    Uses a connect on a UDP socket (no packet is sent) to learn which local
    address the OS would route out of; that is the eth0 LAN IP on a hosted
    runner, distinct from both loopback and the 100.x Tailscale IP.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return None


def _health_ok(host: str, port: int) -> bool:
    import http.client

    try:
        conn = http.client.HTTPConnection(host, port, timeout=3)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            if resp.status != 200:
                return False
            body = json.loads(resp.read())
            return body.get("ok") is True and body.get("service") == "magent-upload"
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


def _child_env() -> dict[str, str]:
    """Child env with no MAGENT_* leakage; PATH kept so the child's own
    ``tailnet.ip4()`` still resolves the real ``tailscale`` binary."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}


# ---------------------------------------------------------------------------
# 1-3. The tailnet.py CLI probes, live
# ---------------------------------------------------------------------------


class TestTailnetProbes:
    def test_ip4_agrees_with_real_tailscale(self, tailnet_wire: str) -> None:
        """`tailnet.ip4()` == the first line of real `tailscale ip -4`, and the
        address is a genuine Tailscale CGNAT (100.64.0.0/10) IPv4."""
        from magent import tailnet

        assert tailnet.ip4() == tailnet_wire
        addr = ipaddress.ip_address(tailnet_wire)
        assert addr in _CGNAT, f"{tailnet_wire} is not a Tailscale CGNAT address"

    def test_probe_reports_live_node(self, tailnet_wire: str) -> None:
        """`tailnet.probe()` sees the binary on PATH, the CLI responding, and
        the same IPv4 -- the three-step doctor probe against a real node."""
        from magent import tailnet

        p = tailnet.probe()
        assert p.on_path is True
        assert p.responding is True
        assert p.ip == tailnet_wire

    def test_magicdns_host_matches_status_json(self, tailnet_wire: str) -> None:
        """`tailnet.magicdns_host()` == real `tailscale status --json`
        Self.DNSName with the trailing dot stripped: a non-empty MagicDNS name,
        never a bare label."""
        from magent import tailnet

        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r.returncode == 0, f"tailscale status --json failed: {r.stderr}"
        raw_dns = (json.loads(r.stdout).get("Self") or {}).get("DNSName", "")
        expected = raw_dns.rstrip(".") if isinstance(raw_dns, str) else ""
        if not expected:
            pytest.skip("this node has no MagicDNS name (MagicDNS disabled on tailnet)")

        host = tailnet.magicdns_host()
        assert host == expected
        assert host and not host.endswith("."), "trailing dot must be stripped"
        assert "." in host, "a MagicDNS name is an FQDN, not a bare label"


# ---------------------------------------------------------------------------
# 4. The serve default bind is fed by the REAL tailnet, wildcard-free
# ---------------------------------------------------------------------------


class TestDefaultBindList:
    def test_default_bind_list_includes_tailscale_never_wildcard(
        self, tailnet_wire: str
    ) -> None:
        """Fed the real `tailnet.ip4()`, `_bind_addresses(None)` is exactly
        loopback + the Tailscale IP -- the LAN wildcard is never auto-chosen."""
        from magent.upload_server import _bind_addresses

        addrs = _bind_addresses(None)
        assert addrs == ["127.0.0.1", tailnet_wire]
        assert "0.0.0.0" not in addrs


# ---------------------------------------------------------------------------
# 5. A real `magent serve` answers /health on the Tailscale IP, and did
#    NOT grab the wildcard
# ---------------------------------------------------------------------------


class TestServeDefaultBind:
    def test_serve_default_bind_answers_on_tailscale_ip(
        self, tmp_path, tailnet_wire: str
    ) -> None:
        """A real `magent serve` with NO --host (the loopback+Tailscale
        default) answers /health on both 127.0.0.1 and the Tailscale IP, and --
        proven live against the running process -- left the LAN wildcard
        unbound (a fresh socket still binds the runner's LAN IP on that port)."""
        port = _free_port()
        cfg = tmp_path / "magent.config.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [{"path": str(proj), "title": "mdtailnet"}],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": "echo mdtailnet"},
                        "uploadServer": False,
                    },
                }
            )
        )

        out_path = tmp_path / "serve.stdout"
        err_path = tmp_path / "serve.stderr"
        # NOTE: no --host, so run_server takes the loopback+Tailscale default.
        with (
            out_path.open("w", encoding="utf-8") as fo,
            err_path.open("w", encoding="utf-8") as fe,
        ):
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "magent",
                    "--config",
                    str(cfg),
                    "serve",
                    "-p",
                    str(port),
                ],
                stdout=fo,
                stderr=fe,
                env=_child_env(),
                cwd=str(tmp_path),
            )
            try:
                # 1. /health answers on loopback (the always-present address).
                if not _wait_until(lambda: _health_ok("127.0.0.1", port), timeout=30):
                    out = out_path.read_text(errors="replace")
                    err = err_path.read_text(errors="replace")
                    pytest.fail(
                        f"serve never healthy on 127.0.0.1:{port} "
                        f"(poll={proc.poll()!r})\nstdout:\n{out}\nstderr:\n{err}"
                    )

                # 2. The headline: /health answers on the real Tailscale IP --
                #    the default bind genuinely opened the tailnet interface.
                assert _wait_until(
                    lambda: _health_ok(tailnet_wire, port), timeout=15
                ), (
                    f"serve default bind is NOT answering /health on the "
                    f"Tailscale IP {tailnet_wire}:{port} -- the tailnet address "
                    f"was not bound\nstderr:\n{err_path.read_text(errors='replace')}"
                )

                # 3. Wildcard-not-bound, proven against the live process: if the
                #    server had bound 0.0.0.0:port, a fresh socket on the
                #    runner's own LAN IP:port would fail EADDRINUSE. A specific
                #    loopback+Tailscale bind leaves it free.
                lan = _lan_ip()
                if lan and lan not in ("127.0.0.1", tailnet_wire):
                    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        probe_sock.bind((lan, port))
                    except OSError as e:  # pragma: no cover - failure path
                        pytest.fail(
                            f"binding {lan}:{port} failed ({e}); the serve "
                            f"default bind appears to hold the LAN wildcard"
                        )
                    finally:
                        probe_sock.close()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
