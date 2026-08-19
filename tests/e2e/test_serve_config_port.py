"""A bare `magent serve` binds the port its CONFIG names -- proven on a real socket.

The defect: `serve --port` defaulted to a hard-coded 8033 and never read
`settings.uploadPort`, while `status`, `up`, `down` and `doctor` all watch the
configured port. On a config that sets anything else, `magent serve` bound a
port nothing else was looking at and every other surface truthfully reported
the upload server dead. It bit this repo's owner twice in one day.

Why this tier and not only the unit pins: the unit tests stop at the argument
`run_server` was handed. What the user actually complained about is which TCP
port a real process ends up listening on, so that is what is asserted here --
a real detached `python -m magent ... serve` with no `-p` at all, probed over
real HTTP.

Isolation copies the sibling `test_altv_flash` / `test_daemon_lifecycle`
fleets, and matters more than usual here because a developer box runs a real
magent fleet: HOME (plus USERPROFILE/HOMEDRIVE/HOMEPATH on Windows) is
redirected into tmp so every `~/.magent` artifact lands there;
MAGENT_HOTKEY_SUPERVISOR=0 keeps this from installing a system-wide keyboard
hook that no HOME redirect contains; the multiplexer is a no-op `psmux` shim on
the child's PATH, never the real binary; the port under test is always one the
OS handed out (bind-0), never a guess; and teardown kills exactly the one pid
this test spawned.

8033 itself is only ever *connect-probed*, never bound -- read-only, and
skipped as an assertion if something on the machine already answers there
(a pre-existing occupant is not this test's process, and asserting on it would
be a false red rather than a finding).
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

# The port serve used to hard-code. Never bound by this test -- only probed.
LEGACY_DEFAULT_PORT = 8033

# Whole-test wall-clock allowances. A blocked wait does not fail a test, it
# burns the job budget and gets the whole job cancelled (see CLAUDE.md), so
# every wait here is bounded.
_READY_TIMEOUT_S = 30.0
_TEARDOWN_TIMEOUT_S = 30.0


def _free_port() -> int:
    """A port the OS just confirmed is free. Never a literal: this machine may
    be running a real magent server, and picking its port would either fail the
    test or -- far worse -- disturb it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connectable(port: int) -> bool:
    """Whether anything accepts a TCP connection on loopback:port. Read-only."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _health_ok(port: int) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200 and json.loads(resp.read()).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


def _install_psmux_shim(shim_dir: Path) -> str:
    """Drop a no-op `psmux` and return its dir, to be prepended to the child's
    PATH. The real psmux on a dev box drives ~40 live agent sessions; nothing
    here may reach it, and the port question needs no multiplexer at all."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (shim_dir / "psmux.bat").write_text(
            "@echo off\r\nexit /b 0\r\n", encoding="utf-8"
        )
    else:
        shim = shim_dir / "psmux"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    return str(shim_dir)


class _Serve:
    """One real detached `magent serve` against a tmp config, fully isolated."""

    def __init__(self, tmp_path: Path, upload_port: int, argv_port: int | None = None):
        unique = uuid.uuid4().hex[:10]
        self.upload_port = upload_port
        self.expected_port = argv_port if argv_port is not None else upload_port
        self.home = tmp_path / f"home-{unique}"
        self.home.mkdir()
        self.shim_path = _install_psmux_shim(tmp_path / f"bin-{unique}")

        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [],
                    "settings": {"uploadServer": True, "uploadPort": upload_port},
                }
            ),
            encoding="utf-8",
        )

        argv = [
            sys.executable,
            "-m",
            "magent",
            "--config",
            str(self.cfg),
            "serve",
            # Loopback only: the default bind would also take the machine's
            # Tailscale IP, and this test has no business on the tailnet.
            "--host",
            "127.0.0.1",
        ]
        if argv_port is not None:
            argv += ["-p", str(argv_port)]
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(),
        )

    def _env(self) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        home_s = str(self.home)
        drive, tail = os.path.splitdrive(home_s)
        env["USERPROFILE"] = home_s
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
        env["HOME"] = home_s
        # A real serve supervises a system-wide keyboard hook into existence;
        # a HOME redirect does not contain one. Opt out rather than install it
        # on the machine running the suite.
        env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
        env["PATH"] = self.shim_path + os.pathsep + env.get("PATH", "")
        return env

    def wait_ready(self) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if _health_ok(self.expected_port):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        self.fail(f"serve never answered /health on 127.0.0.1:{self.expected_port}")

    def pid_file(self, port: int) -> Path:
        """Where THIS server records its pid, inside the redirected HOME."""
        return self.home / ".magent" / f"upload_server-{port}.pid"

    def fail(self, message: str) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        stdout, stderr = self.proc.communicate(timeout=_TEARDOWN_TIMEOUT_S)
        pytest.fail(f"{message}\nstdout:\n{stdout}\nstderr:\n{stderr}")

    def teardown(self) -> None:
        # Exactly the pid this test spawned -- never a port sweep, never a
        # name scan: this machine runs a real magent fleet.
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate(timeout=_TEARDOWN_TIMEOUT_S)


def test_bare_serve_binds_the_configured_upload_port(tmp_path):
    """No `-p` on the command line: the socket must land on the config's port,
    and nothing may appear on the old hard-coded 8033."""
    configured = _free_port()
    assert configured != LEGACY_DEFAULT_PORT  # bind-0 never hands out a used port

    # Read-only, BEFORE spawning: something unrelated may already own 8033 on a
    # developer box, and that is not this test's business to fail over.
    legacy_busy_before = _connectable(LEGACY_DEFAULT_PORT)

    server = _Serve(tmp_path, upload_port=configured)
    try:
        server.wait_ready()

        assert _health_ok(configured), (
            f"serve did not answer /health on the configured port {configured}"
        )
        # Hermetic half: the pid file lands in the redirected HOME, so this
        # cannot be confused by anything else running on the machine.
        assert server.pid_file(configured).exists()
        assert not server.pid_file(LEGACY_DEFAULT_PORT).exists()

        if not legacy_busy_before:
            assert not _connectable(LEGACY_DEFAULT_PORT), (
                f"serve took port {LEGACY_DEFAULT_PORT} instead of the "
                f"configured {configured} -- the regression this pins"
            )
    finally:
        server.teardown()


def test_explicit_port_still_beats_the_config(tmp_path):
    """`-p` is unchanged: an explicit port wins over `settings.uploadPort`."""
    configured = _free_port()
    explicit = _free_port()
    assert configured != explicit

    server = _Serve(tmp_path, upload_port=configured, argv_port=explicit)
    try:
        server.wait_ready()

        assert _health_ok(explicit)
        assert server.pid_file(explicit).exists()
        assert not server.pid_file(configured).exists()
        assert not _health_ok(configured)
    finally:
        server.teardown()
