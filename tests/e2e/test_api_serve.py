"""/api/v1 against a REAL `magent serve` (own process, loopback, HOME in tmp).

What only a real process proves: run_server starts the `magent-events` poller
when MAGENT_EVENTS=1, the SSE response is really streamed (hello, then a
poller-made event, then an event a write caused), and the origin guard sits in
front of a legacy route on the real socket. The multiplexer is a no-op shim
first on PATH (every session reads as not running), so nothing here can reach
a real psmux fleet.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


def _write_noop_psmux(bin_dir) -> None:
    if sys.platform == "win32":
        (bin_dir / "psmux.cmd").write_text("@exit /b 1\r\n", encoding="utf-8")
    else:
        shim = bin_dir / "psmux"
        shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)


class _Serve:
    def __init__(self, tmp_path) -> None:
        self.project = f"mdapi-{uuid.uuid4().hex[:8]}"
        self.home = tmp_path / "home"
        self.home.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_noop_psmux(bin_dir)
        (tmp_path / self.project).mkdir()
        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 4,
                    "projects": [
                        {"path": str(tmp_path / self.project), "title": self.project}
                    ],
                    "settings": {"uploadServer": False},
                }
            ),
            encoding="utf-8",
        )
        self.port = _free_port()
        self.out = tmp_path / "serve.out"
        self.err = tmp_path / "serve.err"
        self._out_fh = self.out.open("w", encoding="utf-8")
        self._err_fh = self.err.open("w", encoding="utf-8")
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        drive, tail = os.path.splitdrive(str(self.home))
        env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            HOMEDRIVE=drive,
            HOMEPATH=tail or "\\",
            PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""),
            # The isolation laws every spawned serve carries ...
            MAGENT_HOTKEY_SUPERVISOR="0",
            MAGENT_UPLOAD_SUPERVISOR="0",
            MAGENT_ATTENTION_SUPERVISOR="0",
            MAGENT_PSMUX_BOOST="0",
            MAGENT_NODE_SYNC="0",
            MAGENT_IDLE_REAP="0",
            MAGENT_SESSION0_POLICY="allow",
            # ... and the one this tier exists to turn ON.
            MAGENT_EVENTS="1",
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(self.cfg),
                "serve",
                "-p",
                str(self.port),
                "--host",
                "127.0.0.1",
            ],
            # Files, not pipes: a pipe nobody drains fills and blocks the
            # child's next write, and the text is wanted only on failure.
            stdout=self._out_fh,
            stderr=self._err_fh,
            env=env,
        )

    def _output(self) -> str:
        return (
            f"stdout:\n{self.out.read_text(encoding='utf-8', errors='replace')}\n"
            f"stderr:\n{self.err.read_text(encoding='utf-8', errors='replace')}"
        )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break  # it died: say so now, not after the deadline
            try:
                if _get(self.port, "/health")[0] == 200:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.stop()
        pytest.fail(
            f"serve never answered /health (rc {self.proc.returncode})\n"
            + self._output()
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=30)
        self._out_fh.close()
        self._err_fh.close()


@pytest.fixture
def serve(tmp_path):
    s = _Serve(tmp_path)
    s.wait_ready()
    yield s
    s.stop()


class _Stream:
    """A raw SSE reader: frames as dicts, each bounded by a deadline."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.sock.sendall(
            f"GET /api/v1/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        )
        self.buf = b""

    def next_event(self, wanted: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n\n", 1)
                fields = dict(
                    line.split(": ", 1)
                    for line in raw.decode().splitlines()
                    if ": " in line and not line.startswith(":")
                )
                if fields.get("event") == wanted:
                    return json.loads(fields["data"])
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                break  # the socket's own timeout: fail with the buffer tail
            if not chunk:
                break
            self.buf += chunk
        pytest.fail(
            f"no {wanted!r} event within {timeout}s; buffer tail {self.buf[-400:]!r}"
        )

    def close(self) -> None:
        self.sock.close()


def test_meta_answers_with_this_servers_epoch(serve):
    status, body = _get(serve.port, "/api/v1/meta")
    assert status == 200
    assert body["data"]["api"] == "v1"
    assert len(body["data"]["epoch"]) == 8


def test_the_stream_says_hello_then_carries_the_pollers_events(serve):
    stream = _Stream(serve.port)
    try:
        hello = stream.next_event("hello")
        assert hello["data"]["sessions"][0]["session"] == serve.project
        # The real poller thread, ticking because this stream subscribed.
        added = stream.next_event("session.added")
        assert added["data"]["session"] == serve.project
        assert added["data"]["live"] is False
    finally:
        stream.close()


def test_a_loopback_write_lands_on_disk_and_on_the_stream(serve):
    stream = _Stream(serve.port)
    try:
        stream.next_event("hello")
        conn = http.client.HTTPConnection("127.0.0.1", serve.port, timeout=10)
        conn.request(
            "PATCH",
            f"/api/v1/projects/{serve.project}",
            body=json.dumps({"enabled": False}),
            headers={"Content-Type": "application/json", "X-Magent-Client": "e2e"},
        )
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        conn.close()
        changed = stream.next_event("project.changed")
        assert changed["data"] == {"name": serve.project, "change": "disabled"}
        saved = json.loads(serve.cfg.read_text(encoding="utf-8"))
        assert saved["projects"][0]["enabled"] is False
    finally:
        stream.close()


def test_a_cross_site_browser_request_is_refused_on_a_legacy_route(serve):
    status, body = _get(
        serve.port,
        "/api/flash?project=x&msg=hi",
        {"Sec-Fetch-Site": "cross-site", "User-Agent": "Mozilla/5.0"},
    )
    assert status == 403
    assert body["reason"] == "cross_site"
