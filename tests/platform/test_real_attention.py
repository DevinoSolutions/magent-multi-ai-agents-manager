"""REAL attention-badge evidence: the actual ``magent attention`` daemon
rewriting the title badges of REAL OS windows as agent-state records change.

The unit suite drives ``AttentionEngine``/``BadgeRenderer`` against a recording
``FakePlatform`` — it proves the diff logic, never that a state change reaches a
live window's title bar. This tier closes that gap with zero fakes: it launches
a real deck (``python -m magent --go``), writes agent-state records exactly
as an external writer would (the pinned ``{state, ts, cwd, session_id}`` schema,
cwd canonicalized through the product's own ``agent_state.norm_cwd``), runs the
real detached ``magent attention -d`` daemon, and then reads each window's
title back through the product's own ``snapshot_windows()`` + ``parse_title`` to
watch the badge appear, advance, and clear.

What the Windows leg proves with zero fakes (win32 only — see the platform note):

* **badge appears.** A ``needs-input`` record for a project makes the daemon
  rewrite that project's window title to ``magent:[!] <name>`` — observed on the
  live HWND via ``EnumWindows``/``GetWindowTextW`` (the same snapshot the
  renderer itself uses), parsed by the shipping ``parse_title``.
* **badge advances (transition).** Overwriting the record with ``error`` flips
  the live title to ``magent:[x] <name>`` on a subsequent daemon tick.
* **badge clears.** Returning the record to ``working`` (an unbadged state)
  restores the clean ``magent:<name>`` title.
* **no cross-talk.** The deck's second window maps to no session; it stays a
  clean ``magent:<name>`` throughout every stage above.
* **heartbeat liveness.** While the daemon is alive its ``attention.heartbeat``
  artifact is present and fresh — the same signal ``status`` reads.

Platform boundary (honest, not a gap we hid): title badges + taskbar flash are
Windows-only in the shipping code — ``Platform.supports_attention_signals()``
returns True only on ``WindowsPlatform`` and only it implements
``set_window_title``/``flash_window``. On Linux/macOS the daemon therefore has
no window renderer to run; the POSIX leg proves that REAL guard end-to-end
(``magent attention -d`` refuses with "badges/flash aren't supported on this
OS" and exits non-zero) and emits a loud ``::warning`` that the live-window
badge proof runs on Windows only. There is no honest Linux/xterm badge to
assert because the product sets no titles there.

Deliberately uncovered (documented, never faked): the taskbar FLASH
(``FlashWindowEx``) is not externally observable, so it is not asserted; the
toast (winotify) and ntfy push channels would hit the OS notification UI / the
network, so they are left OFF (no ``MAGENT_NTFY_TOPIC``) and unasserted.

Isolation & safety (hard rails): every child process — the ``--go`` launch and
the ``attention`` daemon alike — gets a redirected HOME (so its
``~/.magent`` state store / pid / heartbeat / logs and its ``~/.claude``
scans hit ONLY tmp, never the real user's), MAGENT_* stripped, and a
uuid-namespaced shim dir prepended to PATH holding a benign ``claude`` that
records argv and exits (the real agent is NEVER invoked; asserted pre-flight).
Every window title / project name / marker is uuid-namespaced. Cleanup kills the
attention daemon by its exact recorded pid and closes exactly the uuid-named
windows this test created — nothing else on the desktop.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from magent import agent_state
from magent.procs import pid_alive
from magent.titles import parse_title

pytestmark = pytest.mark.platform

_WM_CLOSE = 0x0010


# ---------------------------------------------------------------------------
# Shared helpers (file-local by convention: tests/platform helpers are not
# shared across modules — duplicating the small launch/shim/env helpers from
# the sibling real-launch tier is preferred over editing a shared conftest).
# ---------------------------------------------------------------------------


def _wait_until(check, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _child_env(home: Path, shim_dir: Path) -> dict[str, str]:
    """Real user env, but MAGENT_* stripped, HOME redirected under tmp_path
    (so the child's ~/.magent state/pid/heartbeat/logs and ~/.claude scans
    hit ONLY tmp), and the benign shim dir prepended to PATH."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    home_s = str(home)
    env["HOME"] = home_s
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(home_s)
        env["USERPROFILE"] = home_s
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
    else:
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(home / ".cache")
    return env


def _write_shims(shim_dir: Path, tools: list[str], marker: Path) -> None:
    """Benign executables NAMED like the real agents; each records its argv to
    ``marker`` and stays out of the way (win32: the parent ``cmd /k`` holds the
    window, so the shim just exits; POSIX: exec a bounded sleep)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        if sys.platform == "win32":
            (shim_dir / f"{tool}.cmd").write_text(
                f'@echo off\r\n>>"{marker}" echo {tool}#%*\r\n',
                encoding="utf-8",
            )
        else:
            script = shim_dir / tool
            script.write_text(
                "#!/bin/sh\n"
                f'printf "%s#%s\\n" "{tool}" "$*" >> "{marker}"\n'
                "exec sleep 300\n",
                encoding="utf-8",
            )
            script.chmod(0o755)


def _assert_shim_wins(env: dict[str, str], tool: str, shim_dir: Path) -> None:
    """Pre-flight: prove the benign shim wins PATH resolution, so the real
    ``claude`` can NEVER be invoked even on a box where it is installed."""
    resolved = shutil.which(tool, path=env["PATH"])
    assert resolved is not None, f"{tool!r} shim not resolvable on the child PATH"
    assert str(shim_dir).lower() in resolved.lower(), (
        f"benign {tool!r} shim must win PATH resolution; resolved to {resolved!r}"
    )


def _write_config(cfg_dir: Path, project: Path, name_a: str, name_b: str) -> Path:
    """A 2-window deck for one project. badge/flash are left at their config
    defaults (both on); ``--interval`` drives the daemon's poll cadence."""
    cfg = cfg_dir / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "defaultTool": "claude",
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "happy": False,
                    "tools": {"claude": "claude --continue"},
                },
                "projects": [
                    {
                        "path": str(project),
                        "windows": [{"name": name_a}, {"name": name_b}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _write_state_record(home: Path, cwd: str, state: str, session_id: str) -> None:
    """Write ONE agent-state record exactly as an external writer (state-sink.mjs,
    Codex notify) does: the pinned ``{state, ts, cwd, session_id}`` schema, the
    cwd canonicalized through the SAME public normalizer the engine reads with
    (``agent_state.norm_cwd``), written atomically (tmp + os.replace) under the
    daemon's redirected HOME so a mid-poll read never tears. The filename mirrors
    the store's own sha1 keying so re-writing the same cwd overwrites one record
    (the daemon globs ``*.json`` and maps by the record's ``cwd`` field, so the
    exact name is cosmetic — but a stable name keeps it to one session)."""
    state_dir = home / ".magent" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    norm = agent_state.norm_cwd(cwd)
    # Mirrors agent_state._key (sha1 of the normalized cwd, first 16 hex) so a
    # re-write of the same cwd overwrites one file — not security-sensitive.
    key = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    payload = json.dumps(
        {"state": state, "ts": time.time(), "cwd": norm, "session_id": session_id}
    )
    dest = state_dir / f"{key}.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, dest)


def _read_pid(home: Path) -> int | None:
    """The pid the attention daemon recorded (whether or not it is still alive)."""
    try:
        return int((home / ".magent" / "attention.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _live_daemon_pid(home: Path) -> int | None:
    pid = _read_pid(home)
    return pid if pid and pid_alive(pid) else None


def _heartbeat_fresh(home: Path, max_age: float = 30.0) -> bool:
    """True if the daemon's heartbeat artifact exists and is fresh — the exact
    liveness signal ``status`` reads (log.heartbeat_fresh, HEARTBEAT_MAX_AGE)."""
    hb = home / ".magent" / "attention.heartbeat"
    try:
        age = time.time() - hb.stat().st_mtime
    except OSError:
        return False
    return age <= max_age


# ===========================================================================
# Windows leg — real wt windows + real `attention -d` daemon + live title readback
# ===========================================================================

_skip_not_win = pytest.mark.skipif(
    sys.platform != "win32",
    reason="attention badges are win32-only (see module docstring)",
)
_skip_no_wt = pytest.mark.skipif(
    shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
)


def _states_by_name(plat, names: set[str]) -> dict[str, str | None]:
    """{parsed name: badge-state} for every magent: window currently visible whose
    name is one of ``names``. Absence of a name = that window is not in this
    snapshot; a value of None = present but unbadged (clean magent:<name>)."""
    out: dict[str, str | None] = {}
    for title in plat.snapshot_windows():
        parsed = parse_title(title)
        if parsed is not None and parsed[0] in names:
            out[parsed[0]] = parsed[1]
    return out


def _md_handles_by_name(plat, names: set[str]) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for title, handle in plat.snapshot_windows().items():
        parsed = parse_title(title)
        if parsed is not None and parsed[0] in names:
            out.setdefault(parsed[0], []).append(handle)
    return out


def _await_state(plat, names: set[str], target: str, want: str | None, timeout: float):
    """Wait until ``target``'s window is present with badge-state ``want``
    (``want=None`` means present-and-unbadged); return the whole snapshot at
    that moment so cross-talk on the other window can be asserted consistently."""

    def check():
        states = _states_by_name(plat, names)
        if target in states and states[target] == want:
            return states
        return None

    return _wait_until(check, timeout)


def _win_launch_ready():
    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()
    monitors = plat.list_monitors()
    assert monitors, "no real monitors detected"
    if len(compute_grid(monitors, 2, 1)) < 2:
        pytest.skip("real display cannot host a 2x1 grid (DPI floor collapsed it)")
    return plat


def _run_go(cfg: Path, env: dict[str, str], tmp_path: Path, timeout: float = 120):
    """Run ``magent --go`` to completion, output to FILES not a pipe (a
    launched terminal inherits stdout and would keep a captured pipe open)."""
    out_path = tmp_path / "go.stdout"
    err_path = tmp_path / "go.stderr"
    with (
        out_path.open("w", encoding="utf-8") as fo,
        err_path.open("w", encoding="utf-8") as fe,
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "magent", "--go", "--config", str(cfg)],
            stdout=fo,
            stderr=fe,
            timeout=timeout,
            env=env,
        )
    return (
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def _start_attention_daemon(cfg: Path, env: dict[str, str], tmp_path: Path):
    """Launch ``magent attention -d --interval 1``. The ``-d`` parent spawns
    a detached child (which inherits this redirected env) and returns; its rc is
    tolerated (a slow CI child can miss the parent's ~2s pid wait), so the caller
    polls the pid file. Output to FILES: the detached child inherits std handles,
    so a captured PIPE would hang on the never-closing daemon."""
    out_path = tmp_path / "att.stdout"
    err_path = tmp_path / "att.stderr"
    with (
        out_path.open("w", encoding="utf-8") as fo,
        err_path.open("w", encoding="utf-8") as fe,
    ):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(cfg),
                "attention",
                "-d",
                "--interval",
                "1",
            ],
            stdout=fo,
            stderr=fe,
            timeout=120,
            env=env,
        )
    return (
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


@pytest.fixture
def win_cleanup():
    """Teardown-as-safety-net: kill exactly the attention daemon this test
    started (by its recorded pid) and close exactly the uuid-named windows it
    created, then verify nothing is left. A failed cleanup is a loud teardown
    error, never a leaked daemon/window on the CI desktop."""
    from magent.platform import get_platform

    reg: dict[str, object] = {"home": None, "names": set()}
    yield reg
    plat = get_platform()
    home = reg["home"]
    names = reg["names"]
    leftovers: list[str] = []

    if isinstance(home, Path):
        pid = _read_pid(home)
        if pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )

    if isinstance(names, set) and names:
        for handles in _md_handles_by_name(plat, names).values():
            for hwnd in handles:
                ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        _wait_until(lambda: not _md_handles_by_name(plat, names), timeout=15)
        leftovers += [f"window {n}" for n in _md_handles_by_name(plat, names)]

    if isinstance(home, Path):
        pid = _read_pid(home)
        if pid and pid_alive(pid):
            leftovers.append(f"daemon pid={pid}")

    assert not leftovers, f"cleanup left real windows/processes behind: {leftovers}"


@_skip_not_win
@_skip_no_wt
def test_attention_badges_transition_on_live_windows_win(tmp_path, win_cleanup):
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]

    # The project dir's leaf name IS the session's display name (the engine maps
    # a record's cwd -> get_leaf_name(path)); window A carries that same name so
    # the badge lands on it, window B carries a different uuid name so it maps to
    # no session (the no-cross-talk control).
    name_a = f"mdatt{unique}a"
    name_b = f"mdatt{unique}b"
    project = tmp_path / name_a
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shim_dir = tmp_path / f"bin-{unique}"
    marker = tmp_path / f"marker-{unique}.txt"
    _write_shims(shim_dir, ["claude"], marker)

    cfg = _write_config(tmp_path, project, name_a, name_b)
    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)

    win_cleanup["home"] = home
    win_cleanup["names"] = {name_a, name_b}

    # 1) Real deck: two live wt windows with clean magent: titles, no daemon yet.
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"
    both = _wait_until(
        lambda: len(_md_handles_by_name(plat, {name_a, name_b})) == 2, timeout=30
    )
    assert both, f"expected two live windows {name_a!r} and {name_b!r}"
    pre = _states_by_name(plat, {name_a, name_b})
    assert pre.get(name_a) is None, f"window A should start unbadged: {pre}"
    assert pre.get(name_b) is None, f"window B should start unbadged: {pre}"

    # 2) needs-input record + real daemon -> the [!] badge appears on window A.
    sid = uuid.uuid4().hex
    _write_state_record(home, str(project), agent_state.NEEDS_INPUT, sid)
    drc, dout, derr = _start_attention_daemon(cfg, env, tmp_path)
    daemon_pid = _wait_until(lambda: _live_daemon_pid(home), timeout=30)
    assert daemon_pid, (
        f"attention daemon never wrote a live pid (parent rc={drc})\n"
        f"stdout:\n{dout}\nstderr:\n{derr}"
    )

    s1 = _await_state(plat, {name_a, name_b}, name_a, agent_state.NEEDS_INPUT, 60)
    assert s1, (
        "window A never showed the needs-input badge; "
        f"last snapshot: {_states_by_name(plat, {name_a, name_b})}"
    )
    assert name_b in s1 and s1[name_b] is None, (
        f"window B (no session) must stay unbadged — no cross-talk: {s1}"
    )

    # Heartbeat ride-along: the running daemon's liveness artifact is fresh.
    assert _heartbeat_fresh(home), (
        "attention daemon heartbeat missing/stale while running"
    )

    # 3) Advance the record to error -> the badge flips to [x] on a later tick.
    _write_state_record(home, str(project), agent_state.ERROR, sid)
    s2 = _await_state(plat, {name_a, name_b}, name_a, agent_state.ERROR, 60)
    assert s2, (
        "window A badge never advanced needs-input -> error; "
        f"last snapshot: {_states_by_name(plat, {name_a, name_b})}"
    )
    assert name_b in s2 and s2[name_b] is None, f"window B must stay unbadged: {s2}"

    # 4) Return to an unbadged state (working) -> the badge clears to clean magent:.
    _write_state_record(home, str(project), agent_state.WORKING, sid)
    s3 = _await_state(plat, {name_a, name_b}, name_a, None, 60)
    assert s3, (
        "window A badge never cleared after the state returned to working; "
        f"last snapshot: {_states_by_name(plat, {name_a, name_b})}"
    )
    assert name_b in s3 and s3[name_b] is None, f"window B must stay unbadged: {s3}"


# ===========================================================================
# POSIX leg — the REAL cross-platform guard: badges/flash are win32-only, so
# the daemon must refuse cleanly on Linux/macOS (no window renderer to run).
# ===========================================================================


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX attention-guard leg is non-Windows"
)
def test_attention_daemon_reports_badges_unsupported_posix(tmp_path, capsys):
    """On Linux/macOS ``Platform.supports_attention_signals()`` is False and no
    backend implements ``set_window_title``/``flash_window``, so the badge/flash
    renderers cannot run. This drives the REAL ``magent attention -d`` and
    asserts its shipped guard: it warns that badges/flash are unsupported here
    and exits non-zero (with toast/ntfy off there is nothing left to do). A loud
    ``::warning`` records that the live-window badge proof is Windows-only."""
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cfg = _write_config(tmp_path, project, "mdguard", "mdguard-2")
    env = _child_env(home, bin_dir)

    # No detached child is spawned on this path (the renderer set is empty, so
    # the -d parent exits before spawn_detached), so a captured pipe is safe.
    proc = subprocess.run(
        [sys.executable, "-m", "magent", "--config", str(cfg), "attention", "-d"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, (
        f"attention -d must refuse on {sys.platform} (no attention-signal support); "
        f"got rc=0\n{combined}"
    )
    assert "supported on this OS" in combined, (
        f"expected the unsupported-platform warning on {sys.platform}; got:\n{combined}"
    )

    with capsys.disabled():
        sys.stdout.write(
            "::warning title=attention badge live-window proof is Windows-only::"
            "Title badges + taskbar flash are win32-only "
            "(Platform.supports_attention_signals is True only on WindowsPlatform); "
            "the real live-window badge-transition proof runs on the Windows "
            "platform leg. This POSIX leg proves the daemon's unsupported-platform "
            "guard instead (not a green pass for badge rendering).\n"
        )
        sys.stdout.flush()
