"""The upload server on the wire, started from the INSTALLED ``magent`` entry
point (not ``python -m magent``): a real ``magent serve`` process bound to
a real loopback socket, exercised with real ``http.client`` requests.

What it proves about a ``pip install magent-multi-ai-agents-manager`` user (socket-real, all OSes,
no mocks): the packaged console script starts a server that answers
``/health`` with ``ok: true``, and an oversized ``/upload`` gets the real 413
``{"ok": false, ...}`` JSON envelope on the same connection -- the P4-02
drain-before-error behaviour (PR #44), now verified from the shipped artifact
rather than only the dev tree. Teardown kills the exact pid and confirms the
socket is actually gone.

Isolation: the serve child runs with HOME + the win32 APPDATA config base
redirected into tmp (pid file, uploads, logs all land there), the config is a
tmp file, ``MAGENT_*`` is stripped, and the bind is 127.0.0.1 only. The
``_Serve`` harness is adapted from tests/e2e/test_real_upload.py (light
duplication, per the tier's convention) with the launcher swapped to the
installed entry point.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.dist


def _child_env(home: Path) -> dict[str, str]:
    # PYTHONPATH/PYTHONHOME stripped too: inherited into the pristine venv's
    # interpreter they would splice dev paths back into sys.path.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("MAGENT_")
        and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
    }
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
    return env


def _wait_until(check, timeout: float, interval: float = 0.2):
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


def _health_ok(port: int, errors: list[str] | None = None) -> bool:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            return resp.status == 200 and json.loads(body).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError) as exc:
        if errors is not None:
            errors.append(f"{time.monotonic():.1f}s {type(exc).__name__}: {exc}")
        return False


class _Serve:
    """One real `magent serve` process started from the INSTALLED entry
    point, plus everything needed to talk to it and clean up only it."""

    def __init__(self, packaged, tmp_path: Path):
        self.unique = uuid.uuid4().hex[:10]
        self.health_errors: list[str] = []
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.proj = tmp_path / f"proj-{self.unique}"
        self.proj.mkdir()
        self.env = _child_env(self.home)
        self.port = _free_port()
        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {
                            "path": str(self.proj),
                            "title": f"mddist-up-{self.unique}",
                            "tool": "probe",
                        }
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": f"rem mddist-upload-{self.unique}"},
                        "uploadServer": False,
                        "attention": {
                            "badge": False,
                            "flash": False,
                            "toast": False,
                            "ntfy": False,
                        },
                    },
                }
            )
        )
        self.proc = subprocess.Popen(
            [
                str(packaged.entry_point),
                "--config",
                str(self.cfg),
                "serve",
                "-p",
                str(self.port),
                "--host",
                "127.0.0.1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            cwd=str(self.work),
        )

    def wait_ready(self) -> None:
        started = time.monotonic()
        if _wait_until(lambda: _health_ok(self.port, self.health_errors), timeout=30):
            return
        elapsed = time.monotonic() - started
        state_before_kill = self.proc.poll()
        self.proc.kill()
        stdout, stderr = self.proc.communicate(timeout=30)
        pytest.fail(
            f"installed serve never became healthy on 127.0.0.1:{self.port} "
            f"after {elapsed:.1f}s\n"
            f"proc.poll() before kill: {state_before_kill!r} (None = still running)\n"
            f"last health-check errors: {self.health_errors[-3:]}\n"
            f"{self._diagnostics()}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )

    def _diagnostics(self) -> str:
        md = self.home / ".magent"
        try:
            tree = sorted(str(p.relative_to(md)) for p in md.rglob("*"))
        except OSError as exc:
            tree = [f"<unlistable: {exc}>"]
        log_file = md / "logs" / "upload.log"
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log_text = f"<unreadable: {exc}>"
        return f"~/.magent tree: {tree}\nupload.log:\n{log_text or '<empty>'}"

    def connect(self, timeout: float = 120) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def teardown(self) -> list[str]:
        """Kill exactly the pid we started; return anything left alive."""
        leftovers: list[str] = []
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            leftovers.append(f"serve process pid={self.proc.pid} did not exit")
        # The real socket must actually be gone, not just the process object.
        _wait_until(lambda: not _health_ok(self.port), timeout=10)
        if _health_ok(self.port):
            leftovers.append(f"port {self.port} still serving")
        return leftovers


@pytest.fixture
def serve(packaged, tmp_path):
    srv = _Serve(packaged, tmp_path)
    srv.wait_ready()
    yield srv
    leftovers = srv.teardown()
    assert not leftovers, f"cleanup left real resources behind: {leftovers}"


def test_installed_serve_answers_health(serve):
    """The fixture's wait_ready already polled /health; assert it explicitly so
    a green test names the guarantee: the packaged server is actually SERVING."""
    assert _health_ok(serve.port), "installed serve is not answering /health ok:true"


def test_installed_serve_rejects_oversized_with_413_envelope(serve):
    from magent.upload_server import MAX_UPLOAD_BYTES

    body = b"x" * (MAX_UPLOAD_BYTES + 1)  # honest Content-Length, really sent
    conn = serve.connect()
    try:
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        # P4-02: the server drains the body and the JSON envelope arrives on
        # this same connection -- not a TCP reset (the Windows RST flake #44).
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()

    assert resp.status == 413
    assert json.loads(raw) == {"ok": False, "error": "File too large"}
