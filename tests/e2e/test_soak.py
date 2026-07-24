"""NIGHTLY SOAK tier: long-run daemon stability under sustained realistic churn.

Every other daemon test in the suite runs for seconds; this one runs for tens of
minutes (``MDTEST_SOAK_SECONDS``, default 1500s == 25 min of *active* soak) to
close the honesty-ledger gap "no soak / long-run daemon stability". It stands up
the two long-lived product processes as REAL detached ``python -m magent``
subprocesses -- ``serve --host 127.0.0.1`` (the upload server) and
``attention -d --interval 1`` (the attention daemon) -- and then hammers them
with a continuous churn loop for the whole duration:

  * agent-state records are rewritten every couple of seconds, cycling through
    the entire real state vocabulary (working/done/needs-input/error/idle) across
    several synthetic sessions -- exactly what the daemon's poll->diff->render
    read side consumes every tick;
  * a real multipart POST /upload plus a GET /health round-trip fires
    periodically against the live loopback socket -- the exact wire experience a
    phone or the Alt+V listener gets.

Sampled THROUGHOUT and again AT THE END (minute 25 as well as minute 1):
  * the attention heartbeat mtime keeps advancing and never ages past the
    product's own staleness window (``log.HEARTBEAT_MAX_AGE``) -- no stalls;
  * /health answers 200 and a real upload is accepted (200 + the bytes land
    byte-identical on disk) late in the run just as at the start;
  * both recorded pids stay alive the entire time;
  * process RSS does not grow unboundedly (a leak guard, not a micro-benchmark --
    see ``_assert_no_runaway_rss`` for the threshold rationale);
  * log rotation stays bounded (RotatingFileHandler keeps <= backupCount+1 files
    per logger, each <= its rollover size) -- no runaway logfile;
  * the state store's opportunistic TTL sweep never destroys FRESH records.

Isolation copies the sibling ``test_daemon_lifecycle`` tier verbatim: HOME (and
on Windows USERPROFILE/HOMEDRIVE/HOMEPATH) is redirected into a uuid-namespaced
tmp dir, so every ``~/.magent`` artifact -- pid files, heartbeats, state
records, uploads, logs -- lands there and NEVER touches the runner's real home.
The config is a tmp file passed with ``--config``; servers bind 127.0.0.1 only;
every finally block kills ONLY the exact pids this test created.

The upload-accept path needs a *valid* multiplexer session (the product refuses
uploads to an unknown project). Rather than install a real tmux/psmux, the test
drops a tiny no-op ``psmux`` shim on the child PATH (exits 0 for every verb) --
the same "stand in a real multiplexer" device the browser-upload tier uses with
tmux, reduced to the minimum the upload path exercises: ``has-session`` (session
is live) and ``send-keys`` (inject succeeds). The file is still genuinely parsed
from the multipart body and written to disk by the product before inject; only
the multiplexer behind the session id is faked.

CI-only by design (like ``needs_ssh`` / ``monitor_lab``): gated on
``MDTEST_SOAK=1`` so it never runs in the normal suite or on a dev box -- it
deliberately takes tens of minutes. ``MDTEST_SOAK_SECONDS`` shortens the active
window for local/iteration smoke; the nightly job runs the full default.
"""

from __future__ import annotations

import contextlib
import hashlib
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
from pathlib import Path

import pytest

from magent import log as mlog
from magent.procs import pid_alive

_SOAK_ENABLED = bool(os.environ.get("MDTEST_SOAK"))
# Full active-soak duration in seconds. 1500 == 25 min, comfortably inside the
# 45-min job budget once serve/daemon startup + teardown + churn overhead are
# added. Lower it via the env var for local iteration; the nightly job leaves it
# at the default so the real long-run is what turns the check green.
_SOAK_SECONDS = int(os.environ.get("MDTEST_SOAK_SECONDS", "1500"))

# Churn cadence (seconds). State records rewrite every _CHURN_INTERVAL; the
# heavier upload+health round-trip fires every _UPLOAD_EVERY; RSS is sampled
# every _RSS_EVERY.
_CHURN_INTERVAL = 2.0
_UPLOAD_EVERY = 20.0
_RSS_EVERY = 30.0

# The real state vocabulary, cycled across the synthetic sessions.
_STATES = ("working", "done", "needs-input", "error", "idle")

pytestmark = [
    pytest.mark.soak,
    pytest.mark.skipif(
        not _SOAK_ENABLED,
        reason="soak tier is CI-only (tens of minutes); set MDTEST_SOAK=1 to run",
    ),
]


# --- isolation + polling helpers (mirror test_daemon_lifecycle) ---------------


def _child_env(home, extra_path: str | None = None) -> dict[str, str]:
    """A child env with HOME fully redirected on every OS and every inherited
    MAGENT_* var stripped (the closed env schema can't trip on the runner's
    own config). ``extra_path`` is prepended to PATH so the psmux shim resolves
    inside the serve child."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    return env


def _wait_until(check, timeout: float, interval: float = 0.1):
    """Poll ``check`` until truthy or ``timeout`` elapses."""
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
    """GET /health on loopback -- the zombie-immune 'is the server serving'
    authority (same check status/_upload_state use)."""
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


def _read_pid(path) -> int | None:
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


def _run_detaching(args, env, err_path, timeout: float = 90) -> int:
    """Run a command that DETACHES a long-lived grandchild (``attention -d``).
    stdout -> DEVNULL so the grandchild can't SIGPIPE; stderr -> a file for
    diagnostics. Returns the launcher's exit code."""
    with err_path.open("w", encoding="utf-8") as e:
        p = subprocess.run(
            [sys.executable, "-m", "magent", *args],
            stdout=subprocess.DEVNULL,
            stderr=e,
            env=env,
            timeout=timeout,
        )
    return p.returncode


# --- agent-state writes into the REDIRECTED home ------------------------------
# The test process's own Path.home() is the runner's real home, so we cannot call
# magent.agent_state.write_state (it targets ~/.magent/state). We replicate
# its exact key + atomic-replace contract against the redirected state dir.


def _norm_cwd(path: str) -> str:
    """Byte-identical to agent_state._norm."""
    s = (path or "").replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        s = s.lower()
    return s


def _state_path(state_dir, cwd: str):
    key = hashlib.sha1(_norm_cwd(cwd).encode("utf-8")).hexdigest()[:16]
    return state_dir / f"{key}.json"


def _write_state(state_dir, cwd: str, state: str, session_id: str) -> None:
    """Write one state record with the product's write-then-rename contract."""
    state_dir.mkdir(parents=True, exist_ok=True)
    dest = _state_path(state_dir, cwd)
    tmp = dest.with_suffix(".tmp")
    payload = json.dumps(
        {
            "state": state,
            "ts": time.time(),
            "cwd": _norm_cwd(cwd),
            "session_id": session_id,
        }
    )
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, dest)


# --- psmux shim (stand-in multiplexer so uploads are genuinely accepted) ------


def _install_psmux_shim(shim_dir) -> str:
    """Drop a no-op ``psmux`` on a fresh dir and return that dir (to prepend to
    PATH). Exits 0 for every verb, so the upload path sees the configured project
    as a live session (``has-session``) and inject succeeds (``send-keys``)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        shim = shim_dir / "psmux.bat"
        shim.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        shim = shim_dir / "psmux"
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    return str(shim_dir)


# --- RSS sampling (stdlib only, per-OS) ---------------------------------------


def _rss_kb(pid: int | None) -> int | None:
    """Resident set size of ``pid`` in KiB, or None if unavailable. Linux reads
    /proc/<pid>/status; Windows uses GetProcessMemoryInfo via ctypes; other
    POSIX falls back to ``ps``."""
    if not pid or not pid_alive(pid):
        return None
    if sys.platform.startswith("linux"):
        return _rss_kb_proc(pid)
    if sys.platform == "win32":
        return _rss_kb_windows(pid)
    return _rss_kb_ps(pid)


def _rss_kb_proc(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _rss_kb_ps(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    val = out.stdout.strip()
    try:
        return int(val) if val else None
    except ValueError:
        return None


def _rss_kb_windows(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None

    class _Counters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    try:
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        ):
            return None
        return int(counters.WorkingSetSize) // 1024
    finally:
        kernel32.CloseHandle(handle)


# --- multipart upload ---------------------------------------------------------


def _post_upload(port: int, project: str, data: bytes, filename: str):
    """Real multipart POST /upload. Returns (status, parsed_json_body)."""
    boundary = f"----mdsoak{uuid.uuid4().hex}"
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="project"\r\n\r\n{project}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    post = f"\r\n--{boundary}--\r\n".encode()
    body = pre + data + post
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return resp.status, parsed
    finally:
        conn.close()


# --- world construction -------------------------------------------------------

_PROJECT = "soakproj"  # title == psmux socket id (no chars session_name rewrites)


def _make_world(tmp_path):
    """Redirected HOME + tmp config (one project so the upload path has a live
    session) + free port + psmux shim on PATH."""
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    shim_path = _install_psmux_shim(tmp_path / "shim")
    port = _free_port()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": [{"path": str(proj), "title": _PROJECT, "tool": "probe"}],
                "settings": {
                    "uploadPort": port,
                    "uploadServer": False,
                    "defaultTool": "probe",
                    "tools": {"probe": "rem md-soak"},
                    # toast is the only renderer enable-able on every OS
                    # (badge/flash need real magent: windows + win32 support); with it
                    # on, `attention -d` has work to do and won't exit "nothing to
                    # do". The ToastRenderer swallows a missing winotify (one tip,
                    # then quiet), so cycling push states can't crash the loop.
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
    return types.SimpleNamespace(
        home=home,
        md=home / ".magent",
        env=_child_env(home, extra_path=shim_path),
        port=port,
        cfg=cfg,
        proj=str(proj),
        workdir=tmp_path,
        state_dir=home / ".magent" / "state",
    )


def _start_serve(w):
    """Spawn one real ``serve --host 127.0.0.1``, healthy before returning.
    Returns (proc, recorded_pid). run_server records os.getpid() (the real
    server, a child of the venv trampoline on Windows) -- we key off the pid
    file, never the launcher pid."""
    pidfile = w.md / f"upload_server-{w.port}.pid"
    out_f = (w.workdir / "serve-out.log").open("w", encoding="utf-8")
    err_f = (w.workdir / "serve-err.log").open("w", encoding="utf-8")
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
    if not _wait_until(lambda: _health_ok(w.port), 30):
        err_f.flush()
        raise AssertionError(
            f"serve never became healthy on 127.0.0.1:{w.port}\n"
            f"{(w.workdir / 'serve-err.log').read_text(errors='replace')}"
        )
    server_pid = _read_pid(pidfile)
    assert server_pid is not None and pid_alive(server_pid)
    return proc, server_pid, (out_f, err_f)


def _start_attention(w):
    """Spawn the detached ``attention -d --interval 1`` daemon; return its
    recorded pid once it is alive and pulsing."""
    pidfile = w.md / "attention.pid"
    hb = w.md / "attention.heartbeat"
    rc = _run_detaching(
        ["--config", str(w.cfg), "attention", "-d", "--interval", "1"],
        w.env,
        w.workdir / "att-d.err",
        timeout=60,
    )
    assert rc == 0, (
        "attention -d did not exit 0:\n"
        f"{(w.workdir / 'att-d.err').read_text(errors='replace')}"
    )
    assert _wait_until(lambda: pid_alive(_read_pid(pidfile) or 0), 30), (
        "attention daemon pid never became alive:\n"
        f"{(w.workdir / 'att-d.err').read_text(errors='replace')}"
    )
    daemon_pid = _read_pid(pidfile)
    assert daemon_pid and pid_alive(daemon_pid)
    assert _wait_until(hb.exists, 15), "no heartbeat file was written"
    return daemon_pid


# --- end-of-run invariant checks ----------------------------------------------


def _assert_upload_accepted(w, tag: str) -> None:
    """A real upload lands byte-identical on disk (proves the POST path accepts
    uploads, not just that the socket is open)."""
    payload = f"soak-upload-{tag}-{uuid.uuid4().hex}".encode()
    status, body = _post_upload(w.port, _PROJECT, payload, f"{tag}.txt")
    assert status == 200, f"[{tag}] upload status {status}: {body}"
    assert body.get("ok") is True, f"[{tag}] upload not ok: {body}"
    dest = body.get("path")
    assert dest, f"[{tag}] no path in upload response: {body}"
    landed = Path(dest)
    assert landed.is_file(), f"[{tag}] upload file missing on disk: {dest}"
    assert landed.read_bytes() == payload, f"[{tag}] upload bytes differ on disk"


def _assert_no_runaway_rss(label: str, start_kb: int | None, end_kb: int | None):
    """Leak guard, not a benchmark. A healthy CPython server/daemon settles a few
    tens of MB and stays flat; a genuine leak (per-upload buffers, per-record
    accumulation) climbs into hundreds of MB over 25 min. We flag only a BOTH-
    conditions runaway: end > 1.8x start AND absolute growth > 64 MiB -- generous
    enough to ignore allocator churn / GC high-water jitter, tight enough that a
    real unbounded climb still trips it."""
    if start_kb is None or end_kb is None:
        return  # sampling unavailable on this OS -- other invariants still hold
    grew_ratio = end_kb > start_kb * 1.8
    grew_abs = (end_kb - start_kb) > 64 * 1024
    assert not (grew_ratio and grew_abs), (
        f"{label} RSS runaway: {start_kb} KiB -> {end_kb} KiB "
        f"({(end_kb - start_kb) / 1024:.1f} MiB growth)"
    )


def _assert_logs_bounded(w) -> None:
    """RotatingFileHandler must keep each logger to <= backupCount+1 files, each
    within a rollover of its max size -- no unbounded logfile over the soak."""
    logs = w.md / "logs"
    if not logs.is_dir():
        return
    max_files = mlog._BACKUP_COUNT + 1
    size_ceiling = mlog._MAX_BYTES * 2  # one in-progress file may briefly exceed
    for base in ("upload", "attention"):
        rotated = sorted(logs.glob(f"{base}.log*"))
        assert len(rotated) <= max_files, (
            f"{base} log rotation unbounded: {len(rotated)} files {rotated}"
        )
        for f in rotated:
            assert f.stat().st_size <= size_ceiling, (
                f"{base} log file too large: {f} = {f.stat().st_size} bytes"
            )


def _assert_states_survived(w, cwds) -> None:
    """The daemon's opportunistic TTL sweep (14-day retention) must never destroy
    the FRESH records we kept rewriting."""
    for cwd in cwds:
        p = _state_path(w.state_dir, cwd)
        assert p.is_file(), f"fresh state record vanished: {cwd} -> {p}"
        rec = json.loads(p.read_text(encoding="utf-8"))
        assert rec.get("state") in _STATES, f"corrupt record for {cwd}: {rec}"


# --- the soak -----------------------------------------------------------------


def test_soak_serve_and_attention_stay_healthy(tmp_path):
    w = _make_world(tmp_path)
    up_pidfile = w.md / f"upload_server-{w.port}.pid"
    hb = w.md / "attention.heartbeat"
    # Synthetic sessions the churn loop cycles; the first maps onto the configured
    # project (exercises name-mapping), the rest are unmapped (leaf-name fallback).
    cwds = [w.proj, str(tmp_path / "sess-a"), str(tmp_path / "sess-b")]

    serve_proc = daemon_pid = serve_files = None
    serve_pid = None
    try:
        serve_proc, serve_pid, serve_files = _start_serve(w)
        daemon_pid = _start_attention(w)

        # Minute-1 proof: heartbeat is fresh, health serves, an upload is accepted.
        assert _health_ok(w.port)
        _assert_upload_accepted(w, "minute-1")
        hb_first_mtime = hb.stat().st_mtime
        base_serve_rss = _rss_kb(serve_pid)
        base_daemon_rss = _rss_kb(daemon_pid)

        start = time.monotonic()
        deadline = start + _SOAK_SECONDS
        next_upload = _UPLOAD_EVERY
        next_rss = _RSS_EVERY
        last_serve_rss = base_serve_rss
        last_daemon_rss = base_daemon_rss
        tick = 0

        while time.monotonic() < deadline:
            # 1) churn the state store across the whole vocabulary.
            for j, cwd in enumerate(cwds):
                state = _STATES[(tick + j) % len(_STATES)]
                _write_state(w.state_dir, cwd, state, f"soak-sess-{j}")

            # 2) liveness invariants sampled EVERY tick.
            assert pid_alive(serve_pid), "serve process died mid-soak"
            assert pid_alive(daemon_pid), "attention daemon died mid-soak"
            hb_age = time.time() - hb.stat().st_mtime
            assert hb_age <= mlog.HEARTBEAT_MAX_AGE, (
                f"heartbeat went stale mid-soak: age={hb_age:.1f}s "
                f"> {mlog.HEARTBEAT_MAX_AGE}s (daemon stalled)"
            )

            elapsed = time.monotonic() - start

            # 3) periodic real upload + health round-trip.
            if elapsed >= next_upload:
                assert _health_ok(w.port), f"/health failed at {elapsed:.0f}s"
                _assert_upload_accepted(w, f"t{int(elapsed)}")
                next_upload += _UPLOAD_EVERY

            # 4) periodic RSS sampling.
            if elapsed >= next_rss:
                s = _rss_kb(serve_pid)
                d = _rss_kb(daemon_pid)
                if s is not None:
                    last_serve_rss = s
                if d is not None:
                    last_daemon_rss = d
                next_rss += _RSS_EVERY

            tick += 1
            time.sleep(_CHURN_INTERVAL)

        # --- end-of-run (minute-25) invariants --------------------------------
        assert pid_alive(serve_pid), "serve did not survive the full soak"
        assert pid_alive(daemon_pid), "attention daemon did not survive the soak"

        # health + upload accepted just as at minute 1.
        assert _health_ok(w.port), "server stopped serving by end of soak"
        _assert_upload_accepted(w, "minute-25")

        # heartbeat advanced across the whole run and is still fresh.
        hb_last_mtime = hb.stat().st_mtime
        if _SOAK_SECONDS > mlog.HEARTBEAT_INTERVAL:
            assert hb_last_mtime > hb_first_mtime, (
                "heartbeat mtime never advanced over the soak (daemon not pulsing)"
            )
        assert (time.time() - hb_last_mtime) <= mlog.HEARTBEAT_MAX_AGE

        # bounded resources + surviving fresh records.
        _assert_no_runaway_rss("serve", base_serve_rss, last_serve_rss)
        _assert_no_runaway_rss("attention", base_daemon_rss, last_daemon_rss)
        _assert_logs_bounded(w)
        _assert_states_survived(w, cwds)
    finally:
        # Exact-target teardown: only the pids WE created.
        _kill_pid(serve_pid)  # the recorded server pid
        if serve_proc is not None:
            _kill_pid(serve_proc.pid)  # the launcher/trampoline
            with contextlib.suppress(subprocess.TimeoutExpired):
                serve_proc.wait(timeout=30)
        if serve_files is not None:
            for f in serve_files:
                f.close()
        _kill_pid(daemon_pid)
        if daemon_pid:
            _wait_until(lambda: not pid_alive(daemon_pid), 15)
        _wait_until(lambda: not _health_ok(w.port), 10)

    # --- post-teardown: nothing this test started is left running -------------
    assert not _health_ok(w.port), "a serve process is still bound to the port"
    assert not up_pidfile.exists() or not pid_alive(_read_pid(up_pidfile) or 0), (
        "the upload server survived teardown"
    )
    if daemon_pid:
        assert not pid_alive(daemon_pid), "the attention daemon survived teardown"
