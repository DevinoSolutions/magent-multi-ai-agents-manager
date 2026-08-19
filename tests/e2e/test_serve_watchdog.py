"""The upload server has a supervisor now, and this tier is the proof.

`magent serve` is what every mobile upload and every Alt+V press goes through.
Everything else in the product is supervised -- attach panes redial, sessions
get revived, the Alt+V listener is kept alive by serve itself -- but serve had
no supervisor, and when it died it left no trace. It died silently twice in one
day; both times the first symptom was a human pressing Alt+V and getting
nothing. `attention -d` is now its supervisor.

Nothing here is faked between the daemon and the server:

    REAL detached `magent attention -d --interval 1`  ->  its REAL per-tick
    liveness probe  ->  a REAL detached `magent serve` OS process, bound to a
    real loopback socket, answering a real GET /health.

Two facts are pinned, in the order they matter:

  1. with no server running at all, the daemon starts one on the CONFIGURED
     port (the incident: a serve that vanished and stayed vanished);
  2. kill that server and a DIFFERENT one takes its place (the incident's
     other half: dying once must not be permanent), no sooner than the
     respawn cooldown allows.

Isolation copies the sibling daemon tiers verbatim. HOME (and on Windows
USERPROFILE/HOMEDRIVE/HOMEPATH) is redirected into a uuid-namespaced tmp dir,
so every ~/.magent artifact -- pid files, heartbeats, logs -- lands there and
never touches the runner's real home. A REAL executable named `psmux` on the
child PATH stands in for the multiplexer so nothing can reach a real one. The
config is a tmp file passed with --config; the port comes from a bind-0 lease,
never a well-known one. MAGENT_HOTKEY_SUPERVISOR=0 because the Alt+V listener
installs a SYSTEM-WIDE keyboard hook that no HOME redirect contains -- and
proving that the REVIVED server inherited that opt-out is itself an assertion
here, since the daemon spawns it. Teardown kills only pids this test created,
found through the redirected home's pid files.

Every wait is bounded and the whole test is clamped by a single wall-clock
budget: a blocked wait does not fail a test, it burns the job's
`timeout-minutes` until GitHub CANCELS the job and discards every diagnostic
(that has happened to this repo -- see tests/e2e/_pty.py's Budget).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from magent.procs import pid_alive

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

# The supervisor's own cooldown, shortened for this test through the documented
# env knob. The production default is 60s (launch.UPLOAD_RESPAWN_COOLDOWN_S);
# waiting it out twice would put a minute of pure sleep in every CI run on three
# OSes to prove a rule that is unit-pinned at its real value anyway.
_COOLDOWN_S = 3.0

# Per-stage allowances. Generous for a cold Windows runner (a first `python -m
# magent` spawn pays image load + AV scan), tight enough that the sum stays far
# under the job's timeout.
_DAEMON_UP_S = 60.0
_REVIVE_S = 60.0

# The whole-test clamp: no sequence of stages may exceed it, whatever each one
# individually allows.
_BUDGET_S = 180.0


class _Budget:
    """One wall-clock allowance for the entire test.

    Per-stage timeouts sum to a worst case nobody chose; this makes the total
    the number that is actually enforced. Every wait below runs under it, so a
    stage that starts late gets the time that is left, not its full share.
    """

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self._deadline - time.monotonic()

    def allow(self, seconds: float) -> float:
        remaining = self.left()
        if remaining <= 0:
            pytest.fail("test budget exhausted before the stage could start")
        return min(seconds, remaining)


def _wait_until(check, timeout: float, interval: float = 0.25):
    """Poll ``check`` until truthy or ``timeout`` elapses. Never unbounded."""
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


def _health_ok(port: int) -> bool:
    """GET /health on loopback -- the zombie-immune 'is it SERVING' authority,
    not merely 'is something bound'."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            return resp.status == 200 and json.loads(body).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kill_pid(pid: int | None) -> None:
    """Kill exactly one pid (its tree, on Windows) and tolerate it already being
    gone. Never raises. Only ever called with a pid this test created."""
    if not pid:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def _install_psmux_shim(shim_dir: Path) -> str:
    """A REAL executable named ``psmux`` that does nothing, on a dir of its own.

    Prepended to the child PATH so no code path on this machine can resolve the
    developer's (or the runner's) real multiplexer. The watchdog never needs
    one; this is the fence, not a fixture.
    """
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


class _World:
    """A redirected HOME, a leased port, a fake multiplexer, and the env that
    ties them to a child process."""

    def __init__(self, tmp_path: Path) -> None:
        self.unique = uuid.uuid4().hex[:10]
        self.home = tmp_path / f"home-{self.unique}"
        self.home.mkdir()
        self.md = self.home / ".magent"
        self.workdir = tmp_path
        self.proj = tmp_path / f"proj-{self.unique}"
        self.proj.mkdir()
        shim_path = _install_psmux_shim(tmp_path / "shim")
        self.port = _free_port()
        self.cfg = tmp_path / "magent.config.json"
        self.cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {
                            "path": str(self.proj),
                            "title": f"mdwd-{self.unique}",
                            "tool": "probe",
                        }
                    ],
                    "settings": {
                        # The switch the supervisor honours: a config with no
                        # upload server is never second-guessed, so the tier
                        # that proves it revives one has to turn it on.
                        "uploadServer": True,
                        "uploadPort": self.port,
                        "defaultTool": "probe",
                        "tools": {"probe": "rem md-watchdog"},
                        # toast is the only renderer enable-able on every OS
                        # (badge/flash need real magent: windows + win32
                        # support); with it on, `attention -d` has work to do
                        # and won't refuse with "nothing to do". The
                        # ToastRenderer swallows a missing winotify.
                        "attention": {
                            "badge": False,
                            "flash": False,
                            "toast": True,
                            "ntfy": False,
                            "pollIntervalS": 1.0,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env = self._child_env(shim_path)

    def _child_env(self, shim_path: str) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        home_s = str(self.home)
        drive, tail = os.path.splitdrive(home_s)
        env["USERPROFILE"] = home_s
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
        env["HOME"] = home_s
        # The Alt+V listener installs a SYSTEM-WIDE keyboard hook that no HOME
        # redirect contains. The server under test here is spawned BY the
        # daemon, so this opt-out only holds if the spawn passes the daemon's
        # environment through -- which is asserted, not assumed, below.
        env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
        # The behaviour under test. Left explicit rather than relying on the
        # default, so this file still says what it is exercising if the
        # default ever changes.
        env["MAGENT_UPLOAD_SUPERVISOR"] = "1"
        env["MAGENT_UPLOAD_RESPAWN_COOLDOWN_S"] = str(_COOLDOWN_S)
        env["PATH"] = shim_path + os.pathsep + env.get("PATH", "")
        return env

    @property
    def server_pidfile(self) -> Path:
        return self.md / f"upload_server-{self.port}.pid"

    @property
    def daemon_pidfile(self) -> Path:
        return self.md / "attention.pid"

    def diagnostics(self) -> str:
        """Everything the redirected home knows, for a failure message."""
        lines = [f"port={self.port} home={self.home}"]
        for name in ("att-d.err",):
            path = self.workdir / name
            if path.is_file():
                lines.append(f"--- {name} ---\n{path.read_text(errors='replace')}")
        log = self.md / "logs" / "attention.log"
        if log.is_file():
            lines.append(f"--- attention.log ---\n{log.read_text(errors='replace')}")
        return "\n".join(lines)


def _start_attention(w: _World, budget: _Budget) -> int:
    """Spawn the detached ``attention -d --interval 1``; return its pid.

    stdout -> DEVNULL so the detached grandchild can't SIGPIPE on a closed
    pipe; stderr -> a file, because a launcher that refuses to start is the
    first thing anyone debugging this test needs to read.
    """
    err_path = w.workdir / "att-d.err"
    with err_path.open("w", encoding="utf-8") as err:
        launcher = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(w.cfg),
                "attention",
                "-d",
                "--interval",
                "1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=err,
            env=w.env,
            timeout=budget.allow(_DAEMON_UP_S),
            check=False,
        )
    assert launcher.returncode == 0, f"attention -d did not exit 0:\n{w.diagnostics()}"
    pid = _wait_until(lambda: _read_pid(w.daemon_pidfile), budget.allow(_DAEMON_UP_S))
    assert pid and pid_alive(pid), f"attention daemon never came up:\n{w.diagnostics()}"
    return pid


def _wait_for_serve(w: _World, budget: _Budget, *, not_pid: int | None = None) -> int:
    """Block until a HEALTHY server exists whose pid is not ``not_pid``."""

    def _ready() -> int | None:
        pid = _read_pid(w.server_pidfile)
        if pid is None or pid == not_pid or not _health_ok(w.port):
            return None
        return pid

    pid = _wait_until(_ready, budget.allow(_REVIVE_S))
    assert pid, (
        f"no {'new ' if not_pid else ''}upload server appeared on port {w.port} "
        f"within the budget:\n{w.diagnostics()}"
    )
    return pid


class TestAttentionDaemonSupervisesTheUploadServer:
    def test_it_starts_a_missing_server_and_replaces_a_killed_one(self, tmp_path):
        budget = _Budget(_BUDGET_S)
        w = _World(tmp_path)
        daemon_pid: int | None = None
        seen_servers: list[int] = []
        try:
            # Nothing is listening yet: the port was leased and released.
            assert not _health_ok(w.port)

            daemon_pid = _start_attention(w, budget)

            # (1) A missing server is started, on the CONFIGURED port.
            first = _wait_for_serve(w, budget)
            seen_servers.append(first)
            assert pid_alive(first)

            # The revived server inherited the daemon's environment: no Alt+V
            # listener was installed on the machine running this test. (The
            # listener is Windows-only, so this is a real assertion there and a
            # tautology elsewhere -- which is exactly where the risk lives.)
            assert not (w.md / "hotkey.pid").exists(), (
                "the revived server installed a system-wide keyboard hook; "
                "MAGENT_HOTKEY_SUPERVISOR did not reach the spawned child"
            )
            # ...and it lives entirely inside the redirected home.
            assert w.server_pidfile.is_file()

            # (2) Kill it. This is the incident: the server is gone and nothing
            # about the machine says so.
            #
            # Deliberately NOT asserting that the port goes quiet in between:
            # the supervisor is allowed to be faster than this test can look,
            # and demanding an observable gap would be asserting that the
            # repair is SLOW. What matters is the identity of who answers next.
            _kill_pid(first)

            # A DIFFERENT server takes its place.
            second = _wait_for_serve(w, budget, not_pid=first)
            seen_servers.append(second)
            assert second != first
            assert pid_alive(second)
            assert _health_ok(w.port)
        finally:
            # Only pids this test created: the daemon, plus every server it was
            # observed to spawn (including one that may have appeared after the
            # last assertion).
            _kill_pid(daemon_pid)
            late = _read_pid(w.server_pidfile)
            if late is not None and late not in seen_servers:
                seen_servers.append(late)
            for pid in seen_servers:
                _kill_pid(pid)

    def test_the_opt_out_leaves_the_server_dead(self, tmp_path):
        """MAGENT_UPLOAD_SUPERVISOR=0 is a promise, not a preference: a user who
        runs serve under their own supervisor must not find a second one
        appearing behind them. Same world, same daemon, one env var flipped."""
        budget = _Budget(_BUDGET_S)
        w = _World(tmp_path)
        w.env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
        daemon_pid: int | None = None
        try:
            daemon_pid = _start_attention(w, budget)

            # Give the daemon several poll intervals to do the thing it must
            # not do. Bounded, and deliberately longer than the cooldown.
            time.sleep(budget.allow(_COOLDOWN_S + 4.0))

            assert not _health_ok(w.port), "an opted-out daemon started a server"
            assert not w.server_pidfile.exists()
        finally:
            _kill_pid(daemon_pid)
            _kill_pid(_read_pid(w.server_pidfile))
