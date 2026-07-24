"""Characterization pins for cli/background's phone-URL host resolution
(`_tailnet_host`), written BEFORE the P1-01 tailnet-leaf dedup. Mocks sit at
the subprocess.run / socket boundary so the pins hold whether the Tailscale
probes live inline in background.py or in the shared leaf.

Resolution order under pin: MagicDNS name (trailing dot stripped) -> first
Tailscale IPv4 -> LAN IP -> "localhost".
"""

from __future__ import annotations

import json
import socket
import subprocess

from magent.cli.background import _tailnet_host


def _cp(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class TestTailnetHost:
    def test_magicdns_name_wins_and_trailing_dot_is_stripped(self, monkeypatch):
        # Synthetic hostname: deliberately NOT *.ts.net, so the repo's gitleaks
        # tailscale-magdns-hostname rule can never match a test fixture.
        payload = json.dumps({"Self": {"DNSName": "deck.faketail.example.net."}})

        def _run(args, **_k):
            assert args[:2] == ["tailscale", "status"]
            return _cp(0, payload)

        monkeypatch.setattr(subprocess, "run", _run)
        assert _tailnet_host() == "deck.faketail.example.net"

    def test_falls_back_to_ipv4_when_no_dnsname(self, monkeypatch):
        def _run(args, **_k):
            if args[:2] == ["tailscale", "status"]:
                return _cp(0, json.dumps({"Self": {}}))
            return _cp(0, "100.64.1.2\nfd7a::2\n")

        monkeypatch.setattr(subprocess, "run", _run)
        assert _tailnet_host() == "100.64.1.2"

    def test_falls_back_to_ipv4_on_malformed_status_json(self, monkeypatch):
        def _run(args, **_k):
            if args[:2] == ["tailscale", "status"]:
                return _cp(0, "not json at all")
            return _cp(0, "100.64.1.2\n")

        monkeypatch.setattr(subprocess, "run", _run)
        assert _tailnet_host() == "100.64.1.2"

    def test_falls_back_to_lan_ip_when_tailscale_absent(self, monkeypatch):
        def _run(*_a, **_k):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", _run)
        monkeypatch.setattr(socket, "gethostbyname", lambda _h: "192.168.1.20")
        assert _tailnet_host() == "192.168.1.20"

    def test_localhost_when_everything_fails(self, monkeypatch):
        def _run(*_a, **_k):
            raise FileNotFoundError("tailscale")

        def _no_dns(_h):
            raise OSError("no dns")

        monkeypatch.setattr(subprocess, "run", _run)
        monkeypatch.setattr(socket, "gethostbyname", _no_dns)
        assert _tailnet_host() == "localhost"
