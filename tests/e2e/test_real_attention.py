"""REAL push-on-done (settings.attention.notifyOnDone, PR #43): the attention
loop runs as an actual `python -m magent attention --ticks 2` subprocess,
the agent-state records are written by the REAL ``agent_state.write_state``
(invoked in child processes, exactly like the Claude Code hook writer), and
the ntfy "topic" is a REAL stdlib HTTP server on a real localhost socket
playing the ntfy service's role.

What this proves about the user experience (no fakes inside magent):

* with ``notifyOnDone: true`` + ``ntfy: true``, a session transitioning
  working -> done between two polls produces exactly ONE real HTTP POST to the
  configured MAGENT_NTFY_TOPIC with the documented body ``<name>: done``;
* with ``notifyOnDone`` absent (the default), the very same transition
  produces ZERO pushes -- done stays quiet, needs-input/error remain the only
  push states.

Isolation: the child's HOME/USERPROFILE is redirected into tmp_path, so the
state store, logs, heartbeat and pid file all land there -- never the real
~/.magent. Badge/flash/toast are disabled in config so no real window title
or taskbar is touched. The loop is bounded by the real (hidden) ``--ticks``
flag; no daemon is started.
"""

import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.e2e

_INTERVAL_S = "3.5"  # wide enough to flip the record between tick 1 and tick 2


def _child_env(home, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    env.update(extra)
    return env


def _wait_until(check, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


class _NtfyReceiver(BaseHTTPRequestHandler):
    """The ntfy service's role in this test: a real HTTP endpoint receiving
    real POSTs over a real socket. Records everything it is sent."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.received.append(
            {
                "path": self.path,
                "body": body,
                "title": self.headers.get("Title", ""),
            }
        )
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        """Keep the pytest console clean; the recorder list is the evidence."""


class _ReceiverServer(ThreadingHTTPServer):
    """Receiver with http.server's reverse-DNS server_bind skipped -- the same
    macOS mDNSResponder wedge magent's own _NoFqdnHTTPServer guards
    against (socket.getfqdn can block seconds-to-forever on macOS runners)."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


@pytest.fixture
def ntfy_receiver():
    server = _ReceiverServer(("127.0.0.1", 0), _NtfyReceiver)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def _write_state_via_real_writer(env: dict[str, str], cwd: str, state: str) -> None:
    """Write an agent-state record the way the real hooks do: by invoking
    ``agent_state.write_state`` in a separate process under the redirected
    home, so the record on disk is produced by the real code path."""
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from magent import agent_state; "
            "agent_state.write_state(sys.argv[1], sys.argv[2])",
            cwd,
            state,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert r.returncode == 0, f"real state writer failed: {r.stderr}"


def _setup(tmp_path, port: int, *, notify_on_done: bool):
    unique = uuid.uuid4().hex[:10]
    title = f"mdrl-att-{unique}"
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()

    attention = {
        "badge": False,
        "flash": False,
        "toast": False,
        "ntfy": True,
        "pollIntervalS": 0.2,
    }
    if notify_on_done:
        attention["notifyOnDone"] = True  # the control config leaves it ABSENT

    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": [{"path": str(proj), "title": title}],
                "settings": {"attention": attention},
            }
        )
    )
    topic_path = f"/mdrl-topic-{unique}"
    env = _child_env(home, MAGENT_NTFY_TOPIC=f"http://127.0.0.1:{port}{topic_path}")
    return cfg, env, home, proj, title, topic_path


def _run_two_ticks_flipping_working_to_done(cfg, env, home, proj, title):
    """Seed `working`, run the real CLI for two polls, flip the record to
    `done` between tick 1 and tick 2 (synchronized on the real attention log),
    and return the finished process."""
    _write_state_via_real_writer(env, str(proj), "working")
    state_dir = home / ".magent" / "state"
    assert list(state_dir.glob("*.json")), "real writer left no record on disk"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "magent",
            "--config",
            str(cfg),
            "attention",
            "--ticks",
            "2",
            "--interval",
            _INTERVAL_S,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        # Tick 1 logs the new->working transition; that is our cue that the
        # loop is between ticks and the flip will be observed by tick 2.
        log_file = home / ".magent" / "logs" / "attention.log"

        def _saw_working() -> bool:
            try:
                text = log_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return f"state {title}: new -> working" in text

        assert _wait_until(_saw_working, timeout=30), (
            "attention loop never logged the working transition; "
            f"stderr so far unavailable while running (log: {log_file})"
        )

        _write_state_via_real_writer(env, str(proj), "done")

        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)
    assert proc.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"

    log_text = (home / ".magent" / "logs" / "attention.log").read_text(
        encoding="utf-8", errors="replace"
    )
    assert f"state {title}: working -> done" in log_text, (
        f"tick 2 never saw the done transition:\n{log_text}"
    )
    return stdout, stderr


def test_notify_on_done_pushes_exactly_one_real_post(tmp_path, ntfy_receiver):
    port = ntfy_receiver.server_address[1]
    cfg, env, home, proj, title, topic_path = _setup(
        tmp_path, port, notify_on_done=True
    )

    _run_two_ticks_flipping_working_to_done(cfg, env, home, proj, title)

    received = list(ntfy_receiver.received)
    assert len(received) == 1, f"expected exactly one ntfy POST, got: {received}"
    post = received[0]
    assert post["path"] == topic_path
    assert post["body"] == f"{title}: done".encode()
    assert post["title"] == f"magent: {title}"


def test_default_config_stays_quiet_on_done(tmp_path, ntfy_receiver):
    """The control: notifyOnDone ABSENT (default false) -- the identical
    working -> done transition is seen by the loop but pushes nothing."""
    port = ntfy_receiver.server_address[1]
    cfg, env, home, proj, title, _topic_path = _setup(
        tmp_path, port, notify_on_done=False
    )

    _run_two_ticks_flipping_working_to_done(cfg, env, home, proj, title)

    assert ntfy_receiver.received == [], (
        f"default config must not push on done, got: {ntfy_receiver.received}"
    )
