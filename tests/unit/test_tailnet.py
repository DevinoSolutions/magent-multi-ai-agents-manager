"""Unit tests for the tailnet leaf (P1-01) — the single owner of every
`tailscale` CLI probe. Mocks sit at the subprocess.run / shutil.which
boundary; no real tailscale binary is ever touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from magent import tailnet
from magent.tailnet import TailnetProbe


def _cp(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _raise_fnf(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError("tailscale")


def _raise_timeout(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(cmd="tailscale", timeout=5)


class TestIp4:
    def test_first_ipv4_line_wins(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _cp(0, "100.64.1.2\nfd7a::2\n")
        )
        assert tailnet.ip4() == "100.64.1.2"

    def test_none_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_fnf)
        assert tailnet.ip4() is None

    def test_none_on_timeout(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        assert tailnet.ip4() is None

    def test_none_on_nonzero_returncode(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(1, ""))
        assert tailnet.ip4() is None

    def test_none_on_empty_stdout(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(0, "  \n"))
        assert tailnet.ip4() is None


class TestMagicdnsHost:
    def test_trailing_dot_is_stripped(self, monkeypatch):
        # Synthetic hostname: deliberately NOT *.ts.net, so the repo's gitleaks
        # tailscale-magdns-hostname rule can never match a test fixture.
        payload = json.dumps({"Self": {"DNSName": "deck.faketail.example.net."}})
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(0, payload))
        assert tailnet.magicdns_host() == "deck.faketail.example.net"

    def test_none_when_dnsname_absent(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _cp(0, json.dumps({"Self": {}}))
        )
        assert tailnet.magicdns_host() is None

    def test_none_on_malformed_json(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(0, "not json"))
        assert tailnet.magicdns_host() is None

    def test_none_when_cli_fails(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(1, ""))
        assert tailnet.magicdns_host() is None

    def test_none_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_fnf)
        assert tailnet.magicdns_host() is None


class TestProbe:
    def test_not_on_path(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _cmd: None)
        assert tailnet.probe() == TailnetProbe(on_path=False, responding=False, ip=None)

    def test_on_path_but_not_responding(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/tailscale")
        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        assert tailnet.probe() == TailnetProbe(on_path=True, responding=False, ip=None)

    def test_up_with_ip(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/tailscale")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _cp(0, "100.64.1.2\nfd7a::2\n")
        )
        assert tailnet.probe() == TailnetProbe(
            on_path=True, responding=True, ip="100.64.1.2"
        )

    def test_responding_but_no_ipv4(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/tailscale")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(1, ""))
        assert tailnet.probe() == TailnetProbe(on_path=True, responding=True, ip=None)
