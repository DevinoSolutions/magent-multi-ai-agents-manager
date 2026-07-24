"""REAL daemon/lifecycle tier: `serve`, `serve --ensure`, `attention -d`,
`status`, and `down --all` driven as actual ``python -m magent``
subprocesses against live processes, real loopback sockets, and real pid /
heartbeat / lockfiles on disk. No display, no psmux, no fakes inside
magent -- so this whole module runs on windows, macos AND linux in the
existing e2e job (`pytest tests/e2e/ -m "e2e and not needs_ssh"`).

Everything is isolated the way the sibling real-* e2e tiers do it: HOME (and
on Windows USERPROFILE/HOMEDRIVE/HOMEPATH) is redirected into a uuid-namespaced
tmp dir, so every ``~/.magent`` artifact -- pid files, heartbeats, the
attention lockfile, logs -- lands there and NEVER touches the runner's real
home. The config is a tmp file passed with ``--config``. Servers bind
127.0.0.1 only (the one exception, ``serve --ensure``, is the product's own
loopback+Tailscale default -- never the LAN wildcard; see that test). Every
finally block kills ONLY the exact pids this test created.

What each test really proves (read from source, not assumed):

* test_serve_ensure_idempotent_then_survivor -- a real serve becomes healthy on
  the wire (GET /health == 200) and records its own pid; `serve --ensure` while
  it is alive is a no-op (same pid, no duplicate pid file); after the exact pid
  is killed, `serve --ensure` brings up a genuinely NEW survivor process that
  serves again. This is `_maybe_start_upload_server`'s port-probe idempotence
  and the attach-over-SSH survivor property.
* test_attention_daemonizes_persists_heartbeats_and_dedups -- `attention -d`
  returns while the detached daemon keeps running (its recorded pid is alive
  after the launcher exits); the heartbeat file's mtime ADVANCES across two
  spaced reads (the daemon is really pulsing); a second `attention -d` starts no
  duplicate (same pid). NOTE on the lockfile: `exclusive_lock("attention")`
  guards the CONCURRENT-launch race (LockHeld -> "already in progress"); a
  SEQUENTIAL second launch acquires+releases the lock, then the `daemon_pid()`
  check dedups it -- that is the source path a sequential test can exercise
  deterministically.
* the status truth table -- `status --json`'s exit codes against REAL artifacts:
  0 healthy, 3 degraded (upload "dead" / attention "stale" / attention
  "crashed"), 1 config missing/invalid. Every JSON assertion reads the child's
  stdout (Click merges stderr into `.output`; the version warning would corrupt
  a naive parse -- so the configs stamp version==SCHEMA_VERSION and never warn).
* test_down_all_stops_serve_and_attention_scoped_to_home -- with only pids WE
  created present under HOME, `down --all` kills the real serve and the real
  attention daemon. It is provably HOME-scoped: with zero psmux-eligible
  projects `psmux_status` returns ([], [], []), so `targets` is empty and
  `kill_psmux` is never called -- no global psmux action.

Two product races surfaced while reading source (see the proposed ledger
entries in the PR body), and the assertions are written to be faithful to them:
  - a *dead* pid in the upload pid file reads as "off", not "dead"
    (`_upload_state`), so the degraded-upload cell plants a *live* non-serving
    pid; and
  - `stop_daemon` re-checks `pid_alive` immediately after an async kill, so it
    often reports "not running" and leaves the attention pid/heartbeat behind
    even though the daemon does die -- hence `down --all`'s attention assertion
    is "no live daemon remains", not "pid file removed".
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
import types
import uuid

import pytest

from magent.procs import pid_alive

pytestmark = pytest.mark.e2e


# --- isolation + polling helpers (mirrors the sibling real-* e2e tiers) -------


def _child_env(home, **extra: str) -> dict[str, str]:
    """A child env with HOME fully redirected into ``home`` on every OS, and
    every inherited MAGENT_* var stripped so the closed env schema can't trip
    on the runner's own config."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    env.update(extra)
    return env


def _wait_until(check, timeout: float, interval: float = 0.1):
    """Poll ``check`` until truthy or ``timeout`` elapses. Generous deadlines
    (not fixed sleeps) so slow CI VMs don't flake."""
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
    """GET /health on loopback -- proves the upload server is actually SERVING
    (the same check `status`/`_upload_state` uses to tell "on" from "dead")."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read()
            return resp.status == 200 and json.loads(body).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


def _read_pid(path) -> int | None:
    """Read a pid file the way the product does, but from an EXPLICIT path (the
    product's server_pid()/daemon_pid() resolve Path.home(), which in THIS test
    process is the real home -- so we read the redirected home's files by
    path)."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kill_pid(pid) -> None:
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


def _spawn_dummy() -> subprocess.Popen:
    """A real, live, non-serving process whose pid we can plant in a pid file to
    stage a 'live but not the server' condition (upload "dead" / attention
    "stale")."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _make_world(tmp_path, *, attention=None, projects=None):
    """Build a redirected HOME + tmp config + free port. `attention` overrides
    the settings.attention block; `projects` the projects array (default empty
    -- empty is intentional: it keeps psmux out of every path)."""
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    settings: dict[str, object] = {"uploadPort": port}
    if attention is not None:
        settings["attention"] = attention
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps({"version": 3, "projects": projects or [], "settings": settings}),
        encoding="utf-8",
    )
    return types.SimpleNamespace(
        home=home,
        md=home / ".magent",
        env=_child_env(home),
        port=port,
        cfg=cfg,
        workdir=tmp_path,
    )


def _run(args, env, timeout: float = 90):
    """Run a NON-detaching magent command to completion, capturing output.
    Safe with PIPE: `status`/`down` neither block nor leave a long-lived child
    holding the pipe. Returns (returncode, stdout, stderr)."""
    p = subprocess.run(
        [sys.executable, "-m", "magent", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


def _run_detaching(args, env, err_path, timeout: float = 90) -> int:
    """Run a command that DETACHES a long-lived grandchild (`serve --ensure`,
    `attention -d`). stdout -> DEVNULL so the grandchild, which inherits these
    fds, can never SIGPIPE on a pipe whose reader has gone; stderr -> a file for
    failure diagnostics. Assert on OBSERVABLE state (pid files / health), not on
    captured stdout. Returns the launcher's exit code."""
    with err_path.open("w", encoding="utf-8") as e:
        p = subprocess.run(
            [sys.executable, "-m", "magent", *args],
            stdout=subprocess.DEVNULL,
            stderr=e,
            env=env,
            timeout=timeout,
        )
    return p.returncode


@contextlib.contextmanager
def _serve(w):
    """One real `magent serve --host 127.0.0.1` process, healthy before the
    body runs. Yields the SERVER's recorded pid -- the one run_server writes via
    os.getpid(), which is what status/stop_server target. On a uv/venv Windows
    box that pid differs from the Popen pid (the venv python.exe is a trampoline
    that spawns the real interpreter as a child), so we key off the pid file, not
    the launcher. Killed (recorded pid + launcher tree) afterwards. No
    grandchild: plain serve runs run_server in-process, so file-backed stdio is
    safe."""
    pidfile = w.md / f"upload_server-{w.port}.pid"
    tag = uuid.uuid4().hex[:8]
    out_p = w.workdir / f"serve-out-{tag}.log"
    err_p = w.workdir / f"serve-err-{tag}.log"
    out_f = out_p.open("w", encoding="utf-8")
    err_f = err_p.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "magent",
            "--config",
            str(w.cfg),
            "serve",
            "-p",
            str(w.port),
            "--host",
            "127.0.0.1",
        ],
        stdout=out_f,
        stderr=err_f,
        env=w.env,
    )
    try:
        if not _wait_until(lambda: _health_ok(w.port), 30):
            err_f.flush()
            raise AssertionError(
                f"serve never became healthy on 127.0.0.1:{w.port}\n"
                f"stderr:\n{err_p.read_text(encoding='utf-8', errors='replace')}"
            )
        server_pid = _read_pid(pidfile)
        assert server_pid is not None and pid_alive(server_pid)
        yield server_pid
    finally:
        _kill_pid(_read_pid(pidfile))  # the real server process
        _kill_pid(proc.pid)  # the launcher/trampoline (may already be gone)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
        out_f.close()
        err_f.close()


# --- 1. serve lifecycle: ensure idempotence + survivor ------------------------


def test_serve_ensure_idempotent_then_survivor(tmp_path):
    w = _make_world(tmp_path)
    pidfile = w.md / f"upload_server-{w.port}.pid"
    first = None
    out_f = err_f = None
    pid_a = survivor_pid = None
    try:
        out_p = w.workdir / "serve1-out.log"
        err_p = w.workdir / "serve1-err.log"
        out_f = out_p.open("w", encoding="utf-8")
        err_f = err_p.open("w", encoding="utf-8")
        first = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(w.cfg),
                "serve",
                "-p",
                str(w.port),
                "--host",
                "127.0.0.1",
            ],
            stdout=out_f,
            stderr=err_f,
            env=w.env,
        )
        assert _wait_until(lambda: _health_ok(w.port), 30), (
            "serve never became healthy:\n"
            f"{err_p.read_text(encoding='utf-8', errors='replace')}"
        )
        assert _wait_until(lambda: _read_pid(pidfile) is not None, 10)
        # The recorded pid is run_server's os.getpid() -- the real server
        # process (a child of the venv trampoline on Windows), and the pid every
        # ensure/status/stop path keys off. We target IT, not first.pid.
        pid_a = _read_pid(pidfile)
        assert pid_a is not None and pid_alive(pid_a)

        # `serve --ensure` while alive: the port probe finds the live server, so
        # nothing is spawned -- same pid, still healthy, exactly one pid file.
        rc = _run_detaching(
            ["--config", str(w.cfg), "serve", "--ensure", "-p", str(w.port)],
            w.env,
            w.workdir / "ensure1.err",
        )
        assert rc == 0
        assert _health_ok(w.port)
        assert _read_pid(pidfile) == pid_a, "ensure spawned a duplicate/new pid"
        assert len(list(w.md.glob("upload_server-*.pid"))) == 1

        # Kill the exact pid, then `serve --ensure` must bring up a genuinely NEW
        # survivor. NOTE the bind: the ensure path spawns `serve` WITHOUT --host,
        # i.e. the product's loopback+Tailscale default (never the LAN wildcard;
        # no Tailscale on CI -> loopback only). The health probe is on 127.0.0.1
        # either way.
        # A killed DIRECT-child serve lingers as a POSIX zombie whose pid still
        # answers os.kill(_, 0), so pid_alive would lie here -- the socket
        # (health) is the zombie-immune authority for "the server is gone" (and
        # it is exactly what status/_upload_state check). The zombie is reaped by
        # first.wait() in the finally.
        _kill_pid(pid_a)
        assert _wait_until(lambda: not _health_ok(w.port), 30)

        rc = _run_detaching(
            ["--config", str(w.cfg), "serve", "--ensure", "-p", str(w.port)],
            w.env,
            w.workdir / "ensure2.err",
        )
        assert rc == 0
        assert _wait_until(lambda: _health_ok(w.port), 30), (
            "ensure did not bring up a survivor server:\n"
            f"{(w.workdir / 'ensure2.err').read_text(errors='replace')}"
        )
        survivor_pid = _read_pid(pidfile)
        assert survivor_pid is not None and pid_alive(survivor_pid)
        assert survivor_pid != pid_a, "survivor reused the dead pid"
    finally:
        if first is not None:
            _kill_pid(first.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                first.wait(timeout=30)
        _kill_pid(survivor_pid)
        for f in (out_f, err_f):
            if f is not None:
                f.close()
        _wait_until(lambda: not _health_ok(w.port), 10)
    assert not _health_ok(w.port), "a serve process is still bound to the port"


# --- 2. attention daemonization: persistence + heartbeat + dedup --------------


def test_attention_daemonizes_persists_heartbeats_and_dedups(tmp_path):
    # toast=true is the ONLY renderer _plan_renderers enables regardless of OS
    # (supports_attention_signals() is False on linux/macos, so badge/flash
    # never load there). With zero agent-state records the ToastRenderer never
    # fires and never imports winotify, so the loop is inert and cross-platform.
    w = _make_world(
        tmp_path,
        attention={"badge": False, "flash": False, "toast": True, "ntfy": False},
    )
    pidfile = w.md / "attention.pid"
    hb = w.md / "attention.heartbeat"
    daemon_pid = None
    try:
        rc = _run_detaching(
            ["--config", str(w.cfg), "attention", "-d"],
            w.env,
            w.workdir / "att-d.err",
            timeout=60,
        )
        assert rc == 0, (
            "attention -d did not exit 0:\n"
            f"{(w.workdir / 'att-d.err').read_text(errors='replace')}"
        )
        # The launcher has returned; the DETACHED daemon must persist.
        assert _wait_until(lambda: pid_alive(_read_pid(pidfile) or 0), 30), (
            "attention daemon pid never became alive:\n"
            f"{(w.workdir / 'att-d.err').read_text(errors='replace')}"
        )
        daemon_pid = _read_pid(pidfile)
        assert daemon_pid and pid_alive(daemon_pid)

        # The heartbeat file appears and its mtime ADVANCES -- the run_heartbeat
        # thread pulses every log.HEARTBEAT_INTERVAL (10s), so a >10s window
        # proves the daemon is really alive and pulsing, not just a stale file.
        assert _wait_until(hb.exists, 15), "no heartbeat file was written"
        m0 = hb.stat().st_mtime
        assert _wait_until(lambda: hb.stat().st_mtime > m0, 40), (
            "heartbeat mtime never advanced (daemon not pulsing)"
        )

        # A second sequential `attention -d` refuses to start a duplicate: it
        # acquires+releases the lock, then daemon_pid() finds the live daemon.
        # (The lockfile itself guards only the concurrent-launch race.)
        rc2 = _run_detaching(
            ["--config", str(w.cfg), "attention", "-d"],
            w.env,
            w.workdir / "att-d2.err",
            timeout=60,
        )
        assert rc2 == 0
        assert _read_pid(pidfile) == daemon_pid, "second -d started a duplicate"
        assert pid_alive(daemon_pid)
    finally:
        _kill_pid(daemon_pid)
        if daemon_pid:
            _wait_until(lambda: not pid_alive(daemon_pid), 15)


# --- 3. status truth table against real artifacts -----------------------------


def _status_json(w):
    """Run `status --json` under the world's redirected env; return (rc, data).
    stdout is the JSON (configs stamp version==SCHEMA_VERSION, so no stderr
    version warning corrupts the parse)."""
    rc, out, err = _run(["--config", str(w.cfg), "status", "--json"], w.env)
    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"status --json did not print JSON on stdout (rc={rc})\n"
            f"stdout:\n{out}\nstderr:\n{err}"
        ) from exc
    return rc, data


def test_status_json_healthy_reports_on_and_exits_0(tmp_path):
    w = _make_world(tmp_path)
    with _serve(w):
        rc, data = _status_json(w)
    assert rc == 0, data
    assert data["ok"] is True
    assert data["upload_server"] == "on", data
    # No attention daemon and no Alt+V listener under this fresh HOME.
    assert data["attention"] == "off", data
    assert data["listener"] == "off", data


def test_status_json_upload_dead_reports_dead_and_exits_3(tmp_path):
    w = _make_world(tmp_path)
    # A *dead* pid would read back as "off" (per _upload_state: dead pid + closed
    # port => "off", not degraded). "dead" means a live process (or bound port)
    # that is NOT answering /health -- so plant a REAL live non-serving pid in
    # the upload pid file, leaving the port free.
    dummy = _spawn_dummy()
    try:
        w.md.mkdir(parents=True, exist_ok=True)
        (w.md / f"upload_server-{w.port}.pid").write_text(
            str(dummy.pid), encoding="utf-8"
        )
        rc, data = _status_json(w)
        assert rc == 3, data
        assert data["ok"] is True  # degraded is signalled by state + exit 3
        assert data["upload_server"] == "dead", data
    finally:
        _kill_pid(dummy.pid)


def test_status_json_attention_stale_reports_stale_and_exits_3(tmp_path):
    w = _make_world(tmp_path)
    # "stale" = a LIVE daemon pid whose heartbeat aged past HEARTBEAT_MAX_AGE
    # (30s). A killed daemon reads as "crashed", not "stale" -- so we need a real
    # live pid AND a backdated heartbeat file.
    dummy = _spawn_dummy()
    try:
        w.md.mkdir(parents=True, exist_ok=True)
        (w.md / "attention.pid").write_text(str(dummy.pid), encoding="utf-8")
        beat = w.md / "attention.heartbeat"
        beat.write_text(str(time.time()), encoding="utf-8")
        old = time.time() - 120  # well past the 30s freshness window
        os.utime(beat, (old, old))
        rc, data = _status_json(w)
        assert rc == 3, data
        assert data["attention"] == "stale", data
    finally:
        _kill_pid(dummy.pid)


def test_status_json_attention_crashed_reports_crashed_and_exits_3(tmp_path):
    w = _make_world(tmp_path)
    # "crashed" = a heartbeat with NO live daemon pid (the daemon died without
    # the clean stop that removes the heartbeat). Distinct from "off" (no
    # heartbeat) -- this proves the P6-01 crashed/off distinction on real files.
    w.md.mkdir(parents=True, exist_ok=True)
    (w.md / "attention.heartbeat").write_text(str(time.time()), encoding="utf-8")
    rc, data = _status_json(w)
    assert rc == 3, data
    assert data["attention"] == "crashed", data


def test_status_json_missing_config_exits_1(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _child_env(home)
    missing = tmp_path / "nope.json"
    rc, out, err = _run(["--config", str(missing), "status", "--json"], env)
    assert rc == 1, f"out={out} err={err}"
    assert json.loads(out.strip()) == {"ok": False, "error": "No config found."}


def test_status_json_invalid_config_exits_1(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _child_env(home)
    bad = tmp_path / "magent.config.json"
    bad.write_text("{ this is not valid json ", encoding="utf-8")
    rc, out, err = _run(["--config", str(bad), "status", "--json"], env)
    assert rc == 1, f"out={out} err={err}"
    data = json.loads(out.strip())
    assert data["ok"] is False, data
    assert data.get("error"), data  # ConfigError message surfaced on stdout


# --- 4. down --all against live processes, scoped to HOME ----------------------


def test_down_all_stops_serve_and_attention_scoped_to_home(tmp_path):
    w = _make_world(
        tmp_path,
        attention={"badge": False, "flash": False, "toast": True, "ntfy": False},
    )
    up_pidfile = w.md / f"upload_server-{w.port}.pid"
    att_pidfile = w.md / "attention.pid"
    with _serve(w) as serve_pid:
        assert pid_alive(serve_pid)
        try:
            rc = _run_detaching(
                ["--config", str(w.cfg), "attention", "-d"],
                w.env,
                w.workdir / "down-att.err",
                timeout=60,
            )
            assert rc == 0
            assert _wait_until(lambda: pid_alive(_read_pid(att_pidfile) or 0), 30), (
                "attention daemon never came up:\n"
                f"{(w.workdir / 'down-att.err').read_text(errors='replace')}"
            )
            daemon_pid = _read_pid(att_pidfile)

            # PRECONDITION: every pid file under this HOME is one WE created --
            # so `down --all` cannot possibly reach a foreign process (it only
            # kills the pids recorded in these Path.home()-rooted files).
            found = {p.name: _read_pid(p) for p in sorted(w.md.glob("*.pid"))}
            assert found == {
                f"upload_server-{w.port}.pid": serve_pid,
                "attention.pid": daemon_pid,
            }, found

            rc, out, err = _run(["--config", str(w.cfg), "down", "--all"], w.env)
            assert rc == 0, f"rc={rc}\n{out}\n{err}"

            # The real proof: both real processes end up dead. stop_server
            # unlinks the upload pid file unconditionally on a successful kill;
            # stop_daemon only unlinks after re-confirming death, and because the
            # kill is async that re-check often loses the race -- so we assert
            # "no LIVE daemon remains", not "the pid file was removed".
            # Serve is a DIRECT child here, so a POSIX SIGTERM leaves a zombie
            # whose pid still answers os.kill(_, 0); the socket (health) is the
            # zombie-immune authority for serve death (_serve reaps proc in its
            # finally). The attention daemon is DETACHED (reparented to init and
            # reaped), so its pid_alive stays accurate below.
            assert _wait_until(lambda: not _health_ok(w.port), 30), "serve survived"
            assert _wait_until(
                lambda: _read_pid(att_pidfile) is None or not pid_alive(daemon_pid),
                30,
            ), "attention daemon survived down --all"
            assert _wait_until(lambda: not up_pidfile.exists(), 10)
            assert "stopped upload server" in out.lower(), out
            assert "attention daemon" in out.lower(), out
        finally:
            _kill_pid(_read_pid(att_pidfile))
            leftover = _read_pid(att_pidfile)
            if leftover:
                _wait_until(lambda: not pid_alive(leftover), 15)
    assert not _health_ok(w.port)
