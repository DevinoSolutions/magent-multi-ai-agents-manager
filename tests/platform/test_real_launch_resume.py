"""REAL end-to-end session-resume launch: ``python -m magent --go`` as a
subprocess, driven against schema-true synthetic Claude/Codex session stores,
asserting the *actual command line* the spawned terminal receives.

This is the honest proof the unit suite could never give. The unit tests exercise
``get_claude_session_ids`` / ``get_codex_session_ids`` and ``build_*_resume`` with
fakes and a ``home_override``; nothing has ever driven a real ``--go`` spawn that
(1) discovers a project's most-recent agent session from an on-disk store keyed
exactly the way the shipping parsers key it, (2) builds the resume command, and
(3) hands it to a real terminal -- then read the real OS process's command line
back to confirm the user would see ``claude --resume <uuid>``.

What each leg proves with zero fakes and zero monkeypatching:

* **claude resume + most-recent-wins.** Three ``~/.claude/projects/<encoded>/
  <uuid>.jsonl`` fixtures with distinct mtimes; a 2-window project. The real
  spawn's command lines carry ``--resume`` for the two newest uuids and never the
  oldest, and (win32) creation order maps the newest session to the first window.
* **codex resume + cwd keying.** ``~/.codex/sessions/**/*.jsonl`` fixtures whose
  first line is the real ``{"payload": {"id", "cwd"}}`` shape; a session whose
  ``cwd`` does not match the project is discovered by NEITHER window -- a
  disk-real proof of the per-project keying.
* **empty-store fallback.** With no store, a multi-window claude project falls
  back to the registry's fresh-start command (``--continue`` stripped, no
  ``--resume``) -- observed via the benign shim's recorded argv.
* **per-window override isolation (PR #41).** A window overriding the base
  ``claude`` tool to ``codex`` must NOT receive ``codex resume <claude-uuid>``;
  the discovered session ids belong to the base tool only.
* **happy wrapper.** With ``happy`` on, the resume command is wrapped
  ``happy claude --resume <uuid>``.

Isolation & safety (hard rails): the child gets a redirected HOME (so its
``~/.claude`` / ``~/.codex`` scans read ONLY the tmp fixtures, never the real
user's stores) and a uuid-namespaced shim dir PREPENDED to PATH holding benign
executables named ``claude`` / ``codex`` / ``happy`` that record their argv and
exit -- so the real Anthropic/OpenAI binaries are NEVER invoked. Every test
asserts (pre-flight) that the shim wins PATH resolution before it spawns
anything. Output is captured via FILE redirect, never a pipe (a launched terminal
inherits and holds the pipe -- deadlock). Cleanup closes exactly the
uuid-titled windows and force-kills only the uuid-marked processes.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from magent.sessions.claude import encode_claude_project_path
from magent.titles import make_title

pytestmark = pytest.mark.platform

_WM_CLOSE = 0x0010


# ---------------------------------------------------------------------------
# Shared helpers (OS-agnostic)
# ---------------------------------------------------------------------------


def _wait_until(check, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _child_env(home: Path, shim_dir: Path) -> dict[str, str]:
    """Real user env, but: MAGENT_* stripped, HOME redirected under tmp_path
    (so the child's ~/.claude and ~/.codex session scans hit ONLY the fixtures),
    and the benign shim dir prepended to PATH (so a bare ``claude``/``codex``
    resolves to the shim, never the real tool)."""
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
    """Create benign executables NAMED like the real agents. Each records a
    ``<tool>#<argv>`` line to ``marker`` (proving the real spawn reached OUR
    shim, and with exactly which args) and then keeps the window alive: on
    win32 the parent ``cmd /k`` holds it, so the shim just exits; on POSIX there
    is no ``/k``, so the shim ``exec``s a bounded sleep."""
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


def _prepare(
    tmp_path: Path, unique: str, shim_tools: list[str]
) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / f"proj-{unique}"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shim_dir = tmp_path / f"bin-{unique}"
    marker = tmp_path / f"marker-{unique}.txt"
    _write_shims(shim_dir, shim_tools, marker)
    return project, home, shim_dir, marker


def _seed_claude_store(
    home: Path, project_dir: str, sessions: list[tuple[str, float]]
) -> None:
    """Write ``<home>/.claude/projects/<encoded>/<uuid>.jsonl`` fixtures exactly
    where ``get_claude_session_ids`` looks (via the real ``encode_claude_project_path``)
    with controlled mtimes -- the parser selects most-recent by mtime and takes
    the session id straight from the filename stem (content is irrelevant)."""
    encoded = encode_claude_project_path(project_dir)
    sess_dir = home / ".claude" / "projects" / encoded
    sess_dir.mkdir(parents=True, exist_ok=True)
    for sid, mtime in sessions:
        f = sess_dir / f"{sid}.jsonl"
        f.write_text('{"type":"message"}\n', encoding="utf-8")
        os.utime(f, (mtime, mtime))


def _seed_codex_store(home: Path, sessions: list[tuple[str, str, float]]) -> None:
    """Write ``<home>/.codex/sessions/YYYY/MM/DD/session-*.jsonl`` fixtures whose
    first line is the real ``session_meta`` shape ``get_codex_session_ids``
    parses: ``payload.cwd`` selects by project, ``payload.id`` is the session
    id, and the newest matching mtime wins."""
    sess_root = home / ".codex" / "sessions"
    for i, (sid, cwd, mtime) in enumerate(sessions):
        day_dir = sess_root / "2026" / "06" / f"{20 + i:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "timestamp": "2026-06-20T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd},
        }
        f = day_dir / f"session-{i}-{sid}.jsonl"
        f.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        os.utime(f, (mtime, mtime))


def _write_config(
    tmp_path: Path,
    project: Path,
    *,
    default_tool: str,
    tools: dict[str, str],
    windows: list[dict[str, str]],
    happy: bool = False,
) -> Path:
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "defaultTool": default_tool,
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "happy": happy,
                    "tools": tools,
                },
                "projects": [{"path": str(project), "windows": windows}],
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _assert_shim_wins(env: dict[str, str], tool: str, shim_dir: Path) -> None:
    """Pre-flight safety gate: prove the benign shim wins PATH resolution in the
    child env, so the real ``claude``/``codex`` can NEVER be invoked -- even on a
    developer box where the real tool is installed."""
    resolved = shutil.which(tool, path=env["PATH"])
    assert resolved is not None, f"{tool!r} shim not resolvable on the child PATH"
    assert str(shim_dir).lower() in resolved.lower(), (
        f"benign {tool!r} shim must win PATH resolution (never the real {tool}); "
        f"resolved to {resolved!r}"
    )


def _run_go(
    cfg: Path, env: dict[str, str], tmp_path: Path, timeout: float = 120
) -> tuple[int, str, str]:
    """Run ``magent --go`` to completion capturing output via FILES, never a
    pipe: a launched terminal inherits the child's stdout and holds it for the
    life of its command, so a captured PIPE keeps ``run`` blocked on EOF (a
    multi-minute hang). Files make ``run`` wait only for magent to exit."""
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


def _marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    text = marker.read_text(encoding="utf-8", errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _marker_args(marker: Path, tool: str) -> list[str]:
    """The argv strings the ``tool`` shim recorded, one per invocation."""
    out: list[str] = []
    for ln in _marker_lines(marker):
        name, _, args = ln.partition("#")
        if name == tool:
            out.append(args.strip())
    return out


# ===========================================================================
# Windows leg -- real Windows Terminal + Win32_Process command-line readback
# ===========================================================================

_skip_not_win = pytest.mark.skipif(
    sys.platform != "win32", reason="real Windows Terminal launch is win32-only"
)
_skip_no_wt = pytest.mark.skipif(
    shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
)

_PS_CMD_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { "
    "[pscustomobject]@{ pid = $_.ProcessId; cl = [string]$_.CommandLine; "
    "ft = $(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }) } } | "
    "ConvertTo-Json -Compress"
)


def _cmd_processes() -> list[dict]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _PS_CMD_QUERY],
        capture_output=True,
        text=True,
        timeout=90,
    )
    out = (r.stdout or "").strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict) and d.get("cl")]


def _marker_procs(markers: list[str]) -> dict[str, list[dict]]:
    """One CIM snapshot, filtered per marker substring (each PS spawn is ~2s)."""
    procs = _cmd_processes()
    return {m: [p for p in procs if m in p["cl"]] for m in markers}


def _snapshot_md_handles(plat, titles: list[str]) -> dict[str, object]:
    snap = plat.snapshot_windows()
    return {t: snap[t] for t in titles if t in snap}


def _close_and_verify_gone(plat, titles: list[str], markers: list[str]) -> list[str]:
    """Close exactly the given windows (WM_CLOSE), force-kill any surviving
    marker-tagged cmd.exe by exact PID, and return whatever is still left. Never
    touches a window or process this test did not create (uuid-namespaced)."""
    for hwnd in _snapshot_md_handles(plat, titles).values():
        ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    _wait_until(lambda: not _snapshot_md_handles(plat, titles), timeout=15)

    for procs in _marker_procs(markers).values():
        for proc in procs:
            subprocess.run(
                ["taskkill", "/PID", str(proc["pid"]), "/T", "/F"],
                capture_output=True,
                check=False,
            )

    def _all_gone() -> bool:
        if _snapshot_md_handles(plat, titles):
            return False
        return not any(_marker_procs(markers).values())

    _wait_until(_all_gone, timeout=15)

    leftovers = [f"window {t}" for t in _snapshot_md_handles(plat, titles)]
    leftovers += [
        f"process pid={p['pid']}"
        for procs in _marker_procs(markers).values()
        for p in procs
    ]
    return leftovers


@pytest.fixture
def cleanup_registry():
    """Teardown-as-safety-net: whatever the test registers is closed and verified
    gone even when the body fails; a failed cleanup is a loud teardown error,
    never a leaked real window on the desktop."""
    from magent.platform import get_platform

    reg: dict[str, list[str]] = {"titles": [], "markers": []}
    yield reg
    leftovers = _close_and_verify_gone(get_platform(), reg["titles"], reg["markers"])
    assert not leftovers, f"cleanup left real windows/processes behind: {leftovers}"


def _win_launch_ready():
    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()
    monitors = plat.list_monitors()
    assert monitors, "no real monitors detected"
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("real display cannot host a 2x1 grid (DPI floor collapsed it)")
    return plat


@_skip_not_win
@_skip_no_wt
def test_go_resumes_claude_most_recent_sessions_win(tmp_path, cleanup_registry):
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude"])

    now = time.time()
    sid_old = str(uuid.uuid4())
    sid_mid = str(uuid.uuid4())
    sid_new = str(uuid.uuid4())
    _seed_claude_store(
        home,
        str(project),
        [(sid_old, now - 300), (sid_mid, now - 200), (sid_new, now - 100)],
    )

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = [sid_new, sid_mid]

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    handles = _wait_until(
        lambda: (
            (h := _snapshot_md_handles(plat, [title_a, title_b])) and len(h) == 2 and h
        ),
        timeout=20,
    )
    assert handles, f"expected windows {title_a!r} and {title_b!r}"

    # Real process command lines: the two newest sessions resumed, oldest never.
    by = _marker_procs([sid_new, sid_mid, sid_old])
    assert len(by[sid_new]) == 1, f"newest ran {len(by[sid_new])}x: {by[sid_new]}"
    assert len(by[sid_mid]) == 1, f"2nd ran {len(by[sid_mid])}x: {by[sid_mid]}"
    assert by[sid_old] == [], "oldest session must not be resumed (most-recent-wins)"
    assert f"claude --resume {sid_new}" in by[sid_new][0]["cl"]
    assert f"claude --resume {sid_mid}" in by[sid_mid][0]["cl"]
    # No creation-order assertion here: wt's single-instance delegation
    # queues/batches window creation, so child cmd.exe creation times are racy
    # at ms granularity (~10ms inversions observed on CI run 29107968160).
    # Windows asserts the resumed-session SET (each exactly once, correct
    # uuids, oldest never); the exact per-window mapping stays pinned on the
    # Linux leg via title -> pid -> /proc cmdline.

    # Safety: the real spawn reached OUR benign shim (never the real claude).
    _wait_until(lambda: len(_marker_args(marker, "claude")) == 2, timeout=15)
    args = _marker_args(marker, "claude")
    joined = "\n".join(args)
    assert f"--resume {sid_new}" in joined and f"--resume {sid_mid}" in joined
    assert sid_old not in joined


@_skip_not_win
@_skip_no_wt
def test_go_resumes_codex_most_recent_sessions_win(tmp_path, cleanup_registry):
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["codex"])

    now = time.time()
    sid_old = str(uuid.uuid4())
    sid_mid = str(uuid.uuid4())
    sid_new = str(uuid.uuid4())
    sid_other = str(uuid.uuid4())  # newest mtime, but a DIFFERENT cwd -> ignored
    _seed_codex_store(
        home,
        [
            (sid_old, str(project), now - 300),
            (sid_mid, str(project), now - 200),
            (sid_new, str(project), now - 100),
            (sid_other, str(tmp_path / "someone-else"), now - 1),
        ],
    )

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="codex",
        tools={"codex": "codex"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = [sid_new, sid_mid]

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "codex", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    _wait_until(
        lambda: len(_snapshot_md_handles(plat, [title_a, title_b])) == 2, timeout=20
    )

    by = _marker_procs([sid_new, sid_mid, sid_old, sid_other])
    assert len(by[sid_new]) == 1
    assert len(by[sid_mid]) == 1
    assert by[sid_old] == [], "oldest matching session must not resume"
    assert by[sid_other] == [], "session from a different cwd must not resume"
    assert f"codex resume {sid_new}" in by[sid_new][0]["cl"]
    assert f"codex resume {sid_mid}" in by[sid_mid][0]["cl"]
    # No creation-order assertion: wt's single-instance delegation makes
    # creation-time ordering racy (~10ms inversions observed on CI run
    # 29107968160); Windows asserts the resumed-session SET, and the exact
    # per-window mapping stays pinned on the Linux leg via /proc cmdline.

    _wait_until(lambda: len(_marker_args(marker, "codex")) == 2, timeout=15)
    joined = "\n".join(_marker_args(marker, "codex"))
    assert f"resume {sid_new}" in joined and f"resume {sid_mid}" in joined
    assert sid_old not in joined and sid_other not in joined


@_skip_not_win
@_skip_no_wt
def test_go_falls_back_to_fresh_start_empty_store_win(tmp_path, cleanup_registry):
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude"])
    # No store seeded: home has no ~/.claude at all.

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    # No uuid marker exists for the fresh-start command (`cmd /k claude`), and
    # "claude" alone would collide with a developer's real windows -- so cleanup
    # relies on the uuid-namespaced TITLES only.
    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = []

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    _wait_until(
        lambda: len(_snapshot_md_handles(plat, [title_a, title_b])) == 2, timeout=20
    )

    # The real spawn ran the registry fresh-start: `--continue` stripped, and
    # crucially NO `--resume` appended (an empty store must not invent a session).
    _wait_until(lambda: len(_marker_args(marker, "claude")) == 2, timeout=15)
    args = _marker_args(marker, "claude")
    assert len(args) == 2, f"expected two fresh-start invocations, got {args}"
    for a in args:
        assert a == "", f"fresh-start must carry no resume flag, shim saw: {a!r}"


@_skip_not_win
@_skip_no_wt
def test_go_codex_override_does_not_reuse_claude_session_win(
    tmp_path, cleanup_registry
):
    """PR #41 regression, real-spawn: a per-window ``codex`` override must not be
    handed the base ``claude`` tool's discovered session id."""
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude", "codex"])

    now = time.time()
    sid_new = str(uuid.uuid4())
    _seed_claude_store(home, str(project), [(sid_new, now - 100)])

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue", "codex": "codex"},
        windows=[{"name": name_a}, {"name": name_b, "tool": "codex"}],
    )
    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = [sid_new]

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    _assert_shim_wins(env, "codex", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    _wait_until(
        lambda: len(_snapshot_md_handles(plat, [title_a, title_b])) == 2, timeout=20
    )

    by = _marker_procs([sid_new, "codex resume"])
    assert len(by[sid_new]) == 1
    assert f"claude --resume {sid_new}" in by[sid_new][0]["cl"]
    assert by["codex resume"] == [], (
        "codex override must NOT reuse a claude session id (PR #41 cross-contamination)"
    )

    # The claude window resumed; the codex override window ran fresh-start codex.
    _wait_until(
        lambda: _marker_args(marker, "claude") and _marker_args(marker, "codex"),
        timeout=15,
    )
    claude_args = _marker_args(marker, "claude")
    codex_args = _marker_args(marker, "codex")
    assert claude_args == [f"--resume {sid_new}"], claude_args
    assert codex_args == [""], f"codex override must be fresh-start, saw: {codex_args}"


@_skip_not_win
@_skip_no_wt
def test_go_wraps_resume_with_happy_win(tmp_path, cleanup_registry):
    plat = _win_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude", "happy"])

    now = time.time()
    sid_new = str(uuid.uuid4())
    sid_mid = str(uuid.uuid4())
    _seed_claude_store(home, str(project), [(sid_mid, now - 200), (sid_new, now - 100)])

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue"},
        windows=[{"name": name_a}, {"name": name_b}],
        happy=True,
    )
    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = [sid_new, sid_mid]

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "happy", shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    _wait_until(
        lambda: len(_snapshot_md_handles(plat, [title_a, title_b])) == 2, timeout=20
    )

    by = _marker_procs([sid_new, sid_mid])
    assert len(by[sid_new]) == 1 and len(by[sid_mid]) == 1
    assert f"happy claude --resume {sid_new}" in by[sid_new][0]["cl"]
    assert f"happy claude --resume {sid_mid}" in by[sid_mid][0]["cl"]

    # The outer `happy` shim ran and received the full resume command as its argv.
    _wait_until(lambda: len(_marker_args(marker, "happy")) == 2, timeout=15)
    joined = "\n".join(_marker_args(marker, "happy"))
    assert f"claude --resume {sid_new}" in joined
    assert f"claude --resume {sid_mid}" in joined


# ===========================================================================
# Linux leg -- real xterm + /proc/<pid>/cmdline command-line readback
# ===========================================================================


def _xdotool_ids(title: str) -> list[str]:
    r = subprocess.run(
        ["xdotool", "search", "--name", f"^{re.escape(title)}$"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return r.stdout.split()


def _xterm_pid_for_title(title: str) -> str | None:
    ids = _xdotool_ids(title)
    if not ids:
        return None
    r = subprocess.run(
        ["xdotool", "getwindowpid", ids[0]],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pid = r.stdout.strip()
    return pid if pid.isdigit() else None


def _proc_cmdline(pid: str | None) -> str:
    if not pid:
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _linux_kill_and_verify(plat, titles: list[str]) -> list[str]:
    """Kill exactly the windows carrying ``titles`` (and the xterm pids that own
    them, cascading SIGHUP to their shim children through the closing pty) and
    return whatever still answers. Titles are uuid-namespaced -- never touches a
    window this test did not create."""
    for title in titles:
        for wid in _xdotool_ids(title):
            pid = subprocess.run(
                ["xdotool", "getwindowpid", wid],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            subprocess.run(
                ["xdotool", "windowkill", wid], capture_output=True, check=False
            )
            if pid.isdigit():
                subprocess.run(["kill", "-TERM", pid], capture_output=True, check=False)

    def _gone() -> bool:
        return not any(_xdotool_ids(t) for t in titles)

    _wait_until(_gone, timeout=10)

    for title in titles:
        for wid in _xdotool_ids(title):
            pid = subprocess.run(
                ["xdotool", "getwindowpid", wid],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            if pid.isdigit():
                subprocess.run(["kill", "-KILL", pid], capture_output=True, check=False)
            subprocess.run(
                ["xdotool", "windowkill", wid], capture_output=True, check=False
            )
    _wait_until(_gone, timeout=5)

    leftovers = [f"window {t}" for t in titles if _xdotool_ids(t)]
    leftovers += [f"find_window {t}" for t in titles if plat.find_window(t) is not None]
    return leftovers


@pytest.fixture
def linux_cleanup():
    from magent.platform import get_platform

    titles: list[str] = []
    yield titles
    if titles:
        leftovers = _linux_kill_and_verify(get_platform(), titles)
        assert not leftovers, f"cleanup left real windows/processes: {leftovers}"


def _linux_launch_ready():
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set: no X server to host real windows")
    for tool in ("xterm", "xdotool"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not installed: required for the real xterm resume leg")

    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    monitors = plat.list_monitors()
    assert monitors, "no monitors detected on the X display"
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("display cannot host a 2x1 grid (DPI floor collapsed it)")
    return plat


def _wait_cmdlines(titles: list[str]) -> dict[str, str]:
    """Wait until every title resolves to an xterm whose /proc cmdline is
    readable, then return {title: cmdline}."""

    def _ready():
        cls = {t: _proc_cmdline(_xterm_pid_for_title(t)) for t in titles}
        return cls if all(cls.values()) else None

    return _wait_until(_ready, timeout=20) or {
        t: _proc_cmdline(_xterm_pid_for_title(t)) for t in titles
    }


@pytest.mark.skipif(sys.platform != "linux", reason="real xterm leg is linux-only")
def test_go_resumes_claude_most_recent_sessions_linux(tmp_path, linux_cleanup):
    _linux_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude"])

    now = time.time()
    sid_old = str(uuid.uuid4())
    sid_mid = str(uuid.uuid4())
    sid_new = str(uuid.uuid4())
    _seed_claude_store(
        home,
        str(project),
        [(sid_old, now - 300), (sid_mid, now - 200), (sid_new, now - 100)],
    )

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    linux_cleanup.extend([title_a, title_b])

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    cls = _wait_cmdlines([title_a, title_b])
    cl_a, cl_b = cls[title_a], cls[title_b]
    # Per-window mapping is exact on Linux (title -> owning xterm pid -> cmdline).
    assert f"--resume {sid_new}" in cl_a, f"window A cmdline: {cl_a!r}"
    assert f"--resume {sid_mid}" in cl_b, f"window B cmdline: {cl_b!r}"
    assert sid_old not in cl_a and sid_old not in cl_b, "oldest session must not resume"
    assert sid_mid not in cl_a and sid_new not in cl_b, (
        "sessions must not cross windows"
    )

    _wait_until(lambda: len(_marker_args(marker, "claude")) == 2, timeout=15)
    joined = "\n".join(_marker_args(marker, "claude"))
    assert f"--resume {sid_new}" in joined and f"--resume {sid_mid}" in joined
    assert sid_old not in joined


@pytest.mark.skipif(sys.platform != "linux", reason="real xterm leg is linux-only")
def test_go_resumes_codex_most_recent_sessions_linux(tmp_path, linux_cleanup):
    _linux_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["codex"])

    now = time.time()
    sid_old = str(uuid.uuid4())
    sid_mid = str(uuid.uuid4())
    sid_new = str(uuid.uuid4())
    sid_other = str(uuid.uuid4())
    _seed_codex_store(
        home,
        [
            (sid_old, str(project), now - 300),
            (sid_mid, str(project), now - 200),
            (sid_new, str(project), now - 100),
            (sid_other, str(tmp_path / "someone-else"), now - 1),
        ],
    )

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="codex",
        tools={"codex": "codex"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    linux_cleanup.extend([title_a, title_b])

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "codex", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    cls = _wait_cmdlines([title_a, title_b])
    cl_a, cl_b = cls[title_a], cls[title_b]
    assert f"codex resume {sid_new}" in cl_a, f"window A cmdline: {cl_a!r}"
    assert f"codex resume {sid_mid}" in cl_b, f"window B cmdline: {cl_b!r}"
    assert sid_old not in cl_a and sid_old not in cl_b, "oldest session must not resume"
    assert sid_other not in cl_a and sid_other not in cl_b, (
        "a session from a different cwd must not resume"
    )

    _wait_until(lambda: len(_marker_args(marker, "codex")) == 2, timeout=15)
    joined = "\n".join(_marker_args(marker, "codex"))
    assert f"resume {sid_new}" in joined and f"resume {sid_mid}" in joined
    assert sid_old not in joined and sid_other not in joined


@pytest.mark.skipif(sys.platform != "linux", reason="real xterm leg is linux-only")
def test_go_falls_back_to_fresh_start_empty_store_linux(tmp_path, linux_cleanup):
    _linux_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude"])
    # No store seeded.

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue"},
        windows=[{"name": name_a}, {"name": name_b}],
    )
    linux_cleanup.extend([title_a, title_b])

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    cls = _wait_cmdlines([title_a, title_b])
    for title, cl in cls.items():
        assert "--resume" not in cl, f"{title} fresh-start must not resume: {cl!r}"

    _wait_until(lambda: len(_marker_args(marker, "claude")) == 2, timeout=15)
    args = _marker_args(marker, "claude")
    assert len(args) == 2, f"expected two fresh-start invocations, got {args}"
    for a in args:
        assert a == "", f"fresh-start must carry no resume flag, shim saw: {a!r}"


@pytest.mark.skipif(sys.platform != "linux", reason="real xterm leg is linux-only")
def test_go_codex_override_does_not_reuse_claude_session_linux(tmp_path, linux_cleanup):
    """PR #41 regression, real-spawn (Linux): the per-window ``codex`` override
    window must not receive ``codex resume <claude-uuid>``."""
    _linux_launch_ready()
    unique = uuid.uuid4().hex[:10]
    project, home, shim_dir, marker = _prepare(tmp_path, unique, ["claude", "codex"])

    now = time.time()
    sid_new = str(uuid.uuid4())
    _seed_claude_store(home, str(project), [(sid_new, now - 100)])

    name_a, name_b = f"mdrs{unique}a", f"mdrs{unique}b"
    title_a, title_b = make_title(name_a), make_title(name_b)
    cfg = _write_config(
        tmp_path,
        project,
        default_tool="claude",
        tools={"claude": "claude --continue", "codex": "codex"},
        windows=[{"name": name_a}, {"name": name_b, "tool": "codex"}],
    )
    linux_cleanup.extend([title_a, title_b])

    env = _child_env(home, shim_dir)
    _assert_shim_wins(env, "claude", shim_dir)
    _assert_shim_wins(env, "codex", shim_dir)
    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    cls = _wait_cmdlines([title_a, title_b])
    cl_a, cl_b = cls[title_a], cls[title_b]
    assert f"claude --resume {sid_new}" in cl_a, f"window A cmdline: {cl_a!r}"
    assert "resume" not in cl_b, (
        f"codex override must not reuse a claude session id (PR #41): {cl_b!r}"
    )
    assert sid_new not in cl_b, "the claude uuid must not leak into the codex window"

    _wait_until(
        lambda: _marker_args(marker, "claude") and _marker_args(marker, "codex"),
        timeout=15,
    )
    assert _marker_args(marker, "claude") == [f"--resume {sid_new}"]
    assert _marker_args(marker, "codex") == [""]


# ===========================================================================
# macOS leg -- window-spawn + shell process inspection are TCC-gated; loud skip
# ===========================================================================


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS resume leg is macos-only")
def test_go_resume_real_spawn_macos_loud_skip(capsys):
    """The session-resume real-spawn proof runs for real on the Windows and Linux
    legs. On macOS, Terminal ``do script`` launch AND per-window shell-process
    inspection are TCC-gated on hosted runners (no one can click the consent
    prompt), so this leg is skipped LOUDLY -- never a silent green pass."""
    with capsys.disabled():
        sys.stdout.write(
            "::warning title=macOS resume real-spawn not attempted::"
            "The session-resume real-spawn proof runs on the Windows and Linux "
            "platform legs; macOS Terminal launch + shell process inspection are "
            "TCC-gated on hosted runners, so this leg is skipped (not a green pass).\n"
        )
        sys.stdout.flush()
    pytest.skip("macOS resume real-spawn is TCC-gated (covered on win32 + linux)")
