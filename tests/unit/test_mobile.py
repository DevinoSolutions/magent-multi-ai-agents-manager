"""Characterization pins for cli/mobile's `termius` host resolution, written
BEFORE the P1-01 tailnet-leaf dedup: with no --host, termius asks Tailscale
for an IPv4 and falls back to an interactive prompt. Mocks sit at the
subprocess.run boundary so the pins hold whether the probe lives inline in
mobile.py or in the shared leaf. (`--host`-given behavior is already pinned
by tests/unit/test_cli_smoke.py::test_termius_prints_block.)
"""

from __future__ import annotations

import subprocess

from magent.cli import main


def _cp(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tailscale", "ip", "-4"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestTermiusHostResolution:
    def test_uses_first_tailscale_ipv4_when_no_host_given(self, runner, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _cp(0, "100.64.9.9\nfd7a::9\n")
        )

        result = runner.invoke(main, ["termius", "--user", "u"])

        assert result.exit_code == 0
        assert "HostName 100.64.9.9" in result.output

    def test_prompts_when_tailscale_has_no_answer(self, runner, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(1, ""))

        result = runner.invoke(main, ["termius", "--user", "u"], input="myhost\n")

        assert result.exit_code == 0
        assert "HostName myhost" in result.output
