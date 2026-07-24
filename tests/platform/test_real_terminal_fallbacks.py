"""REAL terminal-fallback tier: prove magent's terminal-emulator SELECTION
chain actually launches real windows for emulators beyond the defaults already
exercised by the #45/#47 tiers, with zero fakes, zero dry-run, zero
monkeypatching.

There is no config knob to pick a terminal (``config.Settings`` has no such
field): the Linux chain is decided purely by ``shutil.which`` order in
``platform/linux.py::launch_terminal`` — kitty → alacritty → gnome-terminal →
konsole → xterm. So the ONLY honest way to prove the chain selects a given
emulator is to make exactly that emulator (and, for the precedence tests, a
chosen subset) the discoverable one on a RESTRICTED PATH, then read back which
real process ended up owning the launched window.

Linux leg (rides the existing Xvfb + openbox platform-integration job; skips
cleanly when DISPLAY is absent):

* **chain-tail render + selection** (``xterm``). A restricted-PATH bin dir
  exposes ONLY xterm plus the honest minimal set of binaries the launch path
  actually shells out to (derived from source: ``xrandr`` for monitor
  detection, ``wmctrl``+``xdotool`` for the tiling snapshot/move, ``sh`` which
  every emulator execs, ``sleep`` which keeps the window alive, and the benign
  ``mdtool`` shim). kitty/alacritty/gnome-terminal/konsole are all made
  UNdiscoverable on that PATH (asserted), so real ``magent --go`` must fall
  the whole ``shutil.which`` chain THROUGH to its tail — and it does: the window
  exists (xdotool), its title parses via ``titles.parse_title`` to the uuid
  project name, the owning process (title → pid → ``/proc/<pid>/comm``) IS
  xterm, the shim marker proves the tool command really ran inside it, and the
  window geometry centres into the ``grid.compute_grid`` cell. This exercises
  the SELECTION chain + owner-process identity that the #45/#47 xterm tiers
  never did (they run with the full ambient PATH and never assert which real
  process owns the window).

Every OTHER chain entry proved untestable on THIS infrastructure — a hosted
runner's headless Xvfb, sharing one CI job with the sibling real-render tests —
for reasons recorded as findings (candidate DESIGN.md ledger items; see the PR
body) rather than worked around with something fragile:

* **kitty** (chain-first) cannot obtain an OpenGL context under headless Xvfb
  even with mesa software-GL — confirmed on CI, it never opens a window.
* **alacritty** is a GPU/OpenGL terminal too; making it render needs
  ``libgl1-mesa-dri`` → libLLVM (~100 MB). Installing that immediately before
  the pytest step churns the runner page cache enough to push the sibling
  real-render tests' first xterm past their tight (<=6s) window-materialization
  deadline (``--go`` returns 0 but the windows miss it) — reproduced 4x on CI.
  Those sibling tests are out of this tier's fence and MUST stay green, so the
  emulator apt-install was removed entirely and alacritty stays out.
* **gnome-terminal / konsole** use a client/server (daemon) model — the window
  is owned by ``gnome-terminal-server`` / the konsole daemon while the client
  exits immediately (breaking the title → pid → comm evidence) — and need a
  session D-Bus that is fragile to fabricate on a headless runner.

Net: beyond-default terminal RENDER on a hosted headless runner is unreachable
without either heavy GPU-emulation deps that destabilise the shared job or a
real GPU / self-hosted runner. xterm is the one chain member that renders on
Xvfb with zero added install, so the Linux leg proves the SELECTION mechanism
there; the Windows leg carries the tier's headline finding. A per-emulator
preflight probe still guards the render (a runner that cannot open xterm at all
yields a loud GitHub ``::warning`` + skip, never a silent green).

Windows leg (rides the existing windows platform-integration job): the honest
finding is that there is NO non-wt fallback — magent fails fast with
actionable guidance rather than launching a degraded console window (TF-W-001).
``platform/windows.py::launch_terminal`` is wt-only; its
``subprocess.Popen(["wt", ...])`` catches the missing-wt ``FileNotFoundError``
and re-raises a typed ``TerminalNotFoundError`` carrying an install hint
(``winget install Microsoft.WindowsTerminal``), which the launch shell prints as
one clean stderr line — NO raw traceback. ``cli/doctor.py`` carries the same
install hint in its terminal check. With wt removed from the child PATH, real
``magent --go`` reaches the launch phase (the grid banner prints), then aborts
non-zero with that hint; no ``magent:`` window is ever created and the tool shim
never runs. ``test_go_no_wt_creates_no_window_win`` PINS this contract so a
future console fallback (a candidate DESIGN.md ledger item — see the PR body)
would break the pin and force a conscious update, rather than shipping
unnoticed.

Isolation & safety (hard rails): every child gets a fully redirected HOME
(``HOME``/``XDG_*`` on POSIX; ``HOME``/``USERPROFILE``/``HOMEDRIVE``/
``HOMEPATH`` on win32) so its ``~/.magent`` and agent-session scans never
touch the real user's; every ``MAGENT_*`` var is stripped; all titles,
configs and markers are uuid-namespaced; the configured tool is a benign shim
that records-and-sleeps — NEVER a real ``claude``/``codex``. Output is captured
via FILE redirect, never a pipe (a launched terminal inherits and holds the
pipe → deadlock). Cleanup closes exactly the uuid-titled windows this test
created (xdotool windowkill / PostMessage WM_CLOSE) and tolerates already-dead
targets; no global psmux kills, no ``down --all``.
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

from magent.grid import compute_grid
from magent.platform import get_platform
from magent.titles import make_title, parse_title

pytestmark = pytest.mark.platform

# Per-edge slack for the geometry check: WM decorations (openbox titlebar/
# border), the terminal's character-cell rounding, and the frame-vs-client
# gravity ambiguity when a WM honours a move+resize. Generous but far below a
# cell width (~2880px on the 5760px Xvfb screen), so it still pins the cell.
_EDGE_TOL = 200

# The Linux terminal chain, verbatim from platform/linux.py::launch_terminal.
# The matrix is xterm only — the one chain member that renders on headless Xvfb
# with zero added install; kitty/alacritty (GPU/GL) and gnome-terminal/konsole
# (daemon-owned windows) are excluded (see module docstring for the full why).
# _CHAIN_ORDER stays complete so the restricted-PATH test can assert none of the
# higher-priority entries are discoverable (i.e. the chain really fell to xterm).
_CHAIN_ORDER = ("kitty", "alacritty", "gnome-terminal", "konsole", "xterm")
_MATRIX = ("xterm",)

# The exact argv each matrix emulator is launched with, mirrored from
# platform/linux.py::launch_terminal. Used ONLY by the preflight capability
# probe; the real assertions always go through magent's own launch_terminal
# via `magent --go`.
_EMULATOR_ARGV = {
    "xterm": lambda title, cwd, cmd: [
        "xterm",
        "-T",
        title,
        "-e",
        f"cd {cwd} && {cmd}",
    ],
}

# The honest minimal set of binaries magent shells out to on the Linux
# launch+tile path, derived from source: xrandr (list_monitors — without it
# --go aborts before launching), wmctrl+xdotool (snapshot_windows/move_window
# tiling), sh (execed by every emulator), sleep (keeps the shim window alive).
_ESSENTIALS = ("sh", "sleep", "xrandr", "wmctrl", "xdotool")


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


def _real_stdout(capsys, line: str) -> None:
    """Write to the *real* step stdout with capture suspended (sys.stdout.write,
    not print(), which T20 bans). GitHub ``::warning`` annotations and auditable
    diagnostics must reach the CI log directly, not captured output."""
    with capsys.disabled():
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_ci_warning(capsys, title: str, message: str) -> None:
    _real_stdout(capsys, f"::warning title={title}::{message}")


def _run_go(cfg: Path, env: dict[str, str], tmp_path: Path, timeout: float = 90):
    """Run ``magent --go`` to completion, capturing output via FILES not a
    pipe: a launched terminal inherits the child's stdout and holds it for the
    life of its command, so a captured PIPE keeps run() blocked on EOF. Returns
    ``(returncode, stdout, stderr)``."""
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
            check=False,
        )
    return (
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def _write_single_window_config(
    tmp_path: Path, name: str, project: Path, tool_cmd: str
) -> Path:
    """A v3, single-window, 2x1-layout config driving a benign shim ``mdtool``.
    2x1 (not 1x1) so the lone window is tiled into the LEFT cell — real proof it
    was moved, not merely left full-screen."""
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "defaultTool": "mdtool",
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {"mdtool": tool_cmd},
                },
                "projects": [{"path": str(project), "windows": [{"name": name}]}],
            }
        ),
        encoding="utf-8",
    )
    return cfg


# ===========================================================================
# Linux legs — real Xvfb + window manager; kitty / alacritty / xterm
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


def _xdotool_name(wid: str) -> str:
    r = subprocess.run(
        ["xdotool", "getwindowname", wid],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return r.stdout.strip()


def _xdotool_pid(wid: str) -> str | None:
    r = subprocess.run(
        ["xdotool", "getwindowpid", wid],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pid = r.stdout.strip()
    return pid if pid.isdigit() else None


def _proc_comm(pid: str | None) -> str:
    if not pid:
        return ""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _proc_cmdline(pid: str | None) -> str:
    if not pid:
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _proc_argv0(pid: str | None) -> str:
    if not pid:
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    first = raw.split(b"\x00", 1)[0]
    return Path(first.decode("utf-8", "replace")).name


def _client_rect(wid: str):
    r = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", wid],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    vals: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k in ("X", "Y", "WIDTH", "HEIGHT"):
                vals[k] = int(v.strip())
    if not {"X", "Y", "WIDTH", "HEIGHT"} <= set(vals):
        return None
    return (vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"])


def _screen_bounds(monitors):
    return (
        min(m.x for m in monitors),
        min(m.y for m in monitors),
        max(m.x + m.w for m in monitors),
        max(m.y + m.h for m in monitors),
    )


def _owner_is(pid: str | None, emulator: str) -> bool:
    """True when the window-owning process IS ``emulator`` — comm match first
    (the mission's title→pid→/proc/comm path), argv0 basename as a fallback for
    a program that rewrites its own comm."""
    return _proc_comm(pid) == emulator or _proc_argv0(pid) == emulator


def _linux_child_env(home: Path, restricted_bin: Path) -> dict[str, str]:
    """Real env minus every MAGENT_* var, HOME + XDG redirected into the
    sandbox, PATH restricted to ONLY ``restricted_bin`` (so no stray emulator
    leaks in), and software-GL forced so kitty/alacritty can open a context
    under headless Xvfb. DISPLAY is inherited from os.environ."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    env["PATH"] = str(restricted_bin)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    # Software rasteriser for the GPU terminals under a GL-less Xvfb.
    env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    env["GALLIUM_DRIVER"] = "llvmpipe"
    return env


def _build_restricted_bin(
    dst: Path, emulators: list[str], marker: Path
) -> tuple[Path, str] | None:
    """Symlink the essentials + the requested emulators into ``dst`` and write
    the benign ``mdtool`` shim (records one line to ``marker`` proving it ran
    inside the terminal, then execs a bounded sleep to hold the window). Returns
    ``(dst, tool_cmd)`` or None if any essential/emulator is missing on PATH."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in (*_ESSENTIALS, *emulators):
        real = shutil.which(name)
        if not real:
            return None
        link = dst / name
        if not link.exists():
            os.symlink(real, link)
    shim = dst / "mdtool"
    shim.write_text(
        f'#!/bin/sh\nprintf "ran %s\\n" "$$" >> "{marker}"\nexec sleep 300\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return dst, "mdtool"


def _emulator_renders(emulator: str, cwd: str, env: dict[str, str]) -> bool:
    """Preflight: can ``emulator`` open a real window under this X server with
    this (software-GL) env? Launches it exactly as launch_terminal would, polls
    for its window, then kills it. Distinguishes a headless-GL-incapable runner
    (→ loud skip) from a genuine magent bug (→ hard fail)."""
    probe_title = f"mdprobe-{uuid.uuid4().hex[:10]}"
    argv = _EMULATOR_ARGV[emulator](probe_title, cwd, "sleep 30")
    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        return bool(_wait_until(lambda: _xdotool_ids(probe_title), timeout=15))
    finally:
        for wid in _xdotool_ids(probe_title):
            subprocess.run(
                ["xdotool", "windowkill", wid], capture_output=True, check=False
            )
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _linux_kill_and_verify(titles: list[str]) -> list[str]:
    """Kill exactly the windows carrying ``titles`` (and the owning emulator
    pids, cascading SIGHUP to their shim children through the closing pty) and
    return whatever still answers. Titles are uuid-namespaced — never touches a
    window this test did not create."""
    plat = get_platform()

    def _sweep(signal: str) -> None:
        for title in titles:
            for wid in _xdotool_ids(title):
                pid = _xdotool_pid(wid)
                subprocess.run(
                    ["xdotool", "windowkill", wid], capture_output=True, check=False
                )
                if pid:
                    subprocess.run(
                        ["kill", signal, pid], capture_output=True, check=False
                    )

    _sweep("-TERM")

    def _gone() -> bool:
        return not any(_xdotool_ids(t) for t in titles)

    _wait_until(_gone, timeout=10)
    _sweep("-KILL")
    _wait_until(_gone, timeout=5)

    leftovers = [f"window {t}" for t in titles if _xdotool_ids(t)]
    leftovers += [f"find_window {t}" for t in titles if plat.find_window(t) is not None]
    return leftovers


@pytest.fixture
def linux_cleanup():
    """Teardown-as-safety-net: whatever the test registers is closed and
    verified gone even if the body fails, so a broken assertion never leaks a
    real terminal. A failed cleanup is a loud teardown error."""
    titles: list[str] = []
    yield titles
    if titles:
        leftovers = _linux_kill_and_verify(titles)
        assert not leftovers, f"cleanup left real windows/processes: {leftovers}"


def _linux_ready():
    """Common Linux-leg preconditions. Returns ``(monitors, slots)`` or skips."""
    if sys.platform != "linux":
        pytest.skip("Linux terminal-fallback leg is linux-only")
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set: no X server to host real windows")
    for tool in ("xdotool", "wmctrl", "xrandr"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not installed: required for the real render leg")
    wm = subprocess.run(["wmctrl", "-m"], capture_output=True, text=True, check=False)
    if wm.returncode != 0:
        pytest.skip(
            "no EWMH window manager on DISPLAY: wmctrl move/resize would no-op "
            "(start one in setup-virtual-displays)"
        )
    monitors = get_platform().list_monitors()
    if not monitors:
        pytest.skip("no monitors detected on the X display")
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("display cannot host a 2x1 grid (DPI floor collapsed it)")
    return monitors, slots


def _launch_and_assert_owner(
    tmp_path: Path,
    capsys,
    linux_cleanup: list[str],
    *,
    on_path: list[str],
    winner: str,
    losers: tuple[str, ...] = (),
    assert_geometry: bool = False,
    monitors=None,
    slots=None,
) -> None:
    """Shared driver: expose ``on_path`` emulators on a restricted PATH, launch
    real ``magent --go``, and assert the launched magent: window is owned by
    ``winner`` (and by none of ``losers``). Optionally assert tiling geometry."""
    unique = uuid.uuid4().hex[:10]
    name = f"mdtf{unique}"
    title = make_title(name)
    project = tmp_path / f"proj-{unique}"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / f"marker-{unique}.txt"
    built = _build_restricted_bin(tmp_path / f"bin-{unique}", on_path, marker)
    if built is None:
        pytest.skip("a required essential/emulator binary is missing on PATH")
    restricted_bin, tool_cmd = built
    env = _linux_child_env(home, restricted_bin)

    # Pre-flight safety: the emulators the launch path can see are EXACTLY the
    # ones we whitelisted — the real terminal, never a stray installed one.
    for probe in _CHAIN_ORDER:
        resolved = shutil.which(probe, path=env["PATH"])
        if probe in on_path:
            assert resolved is not None, f"{probe} must be discoverable on child PATH"
        else:
            assert resolved is None, (
                f"{probe} leaked onto the restricted PATH: {resolved}"
            )

    if not _emulator_renders(winner, str(project), env):
        _emit_ci_warning(
            capsys,
            f"{winner} cannot render headlessly",
            f"{winner} did not open a window under this Xvfb+GL runner "
            f"(software-GL unavailable); the {winner} terminal-fallback leg is "
            f"skipped (not a green pass).",
        )
        pytest.skip(f"{winner} cannot open a window on this runner (headless GL)")

    cfg = _write_single_window_config(tmp_path, name, project, tool_cmd)
    linux_cleanup.append(title)

    rc, out, err = _run_go(cfg, env, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

    wids = _wait_until(lambda: _xdotool_ids(title), timeout=60)
    assert wids, (
        f"expected a real {winner} window titled {title!r}; "
        f"--go stdout:\n{out}\nstderr:\n{err}"
    )
    wid = wids[0]

    # 1. Title round-trips through the product parser to our uuid name.
    parsed = parse_title(_xdotool_name(wid))
    assert parsed is not None and parsed == (name, None), (
        f"window title did not parse to ({name!r}, None): {parsed!r}"
    )

    # 2. The owning process IS the winner emulator, and none of the losers.
    pid = _xdotool_pid(wid)
    _real_stdout(
        capsys,
        f"{winner}: wid={wid} pid={pid} comm={_proc_comm(pid)!r} "
        f"argv0={_proc_argv0(pid)!r}",
    )
    assert _owner_is(pid, winner), (
        f"window owner is not {winner}: comm={_proc_comm(pid)!r} "
        f"argv0={_proc_argv0(pid)!r} cmdline={_proc_cmdline(pid)!r}"
    )
    for loser in losers:
        assert not _owner_is(pid, loser), (
            f"chain picked {loser} but should have picked {winner} "
            f"(order {_CHAIN_ORDER})"
        )

    # 3. magent's real invocation carried our title (proves this pid is the
    #    one magent launched, not an unrelated process).
    assert title in _proc_cmdline(pid), (
        f"owning process cmdline missing {title!r}: {_proc_cmdline(pid)!r}"
    )

    # 4. The tool command really ran INSIDE the terminal (shim marker written).
    assert _wait_until(marker.exists, timeout=15), (
        f"mdtool shim never ran inside the {winner} window (no marker)"
    )

    # 5. Optional: the window was tiled into its computed cell.
    if assert_geometry:
        assert monitors is not None and slots is not None
        rect = _wait_until(lambda: _client_rect(wid), timeout=10)
        assert rect is not None, "could not read window geometry"
        x, y, w, h = rect
        slot = slots[0]
        screen = _screen_bounds(monitors)
        _real_stdout(capsys, f"{winner}: slot={slot} rect={rect} screen={screen}")
        cx, cy = x + w / 2, y + h / 2
        assert abs(x - slot.x) <= _EDGE_TOL, f"left {x} vs slot {slot.x}"
        assert abs(y - slot.y) <= _EDGE_TOL, f"top {y} vs slot {slot.y}"
        assert slot.x <= cx <= slot.x + slot.w, (
            f"center_x {cx} outside cell [{slot.x},{slot.x + slot.w}]"
        )
        assert slot.y <= cy <= slot.y + slot.h, (
            f"center_y {cy} outside cell [{slot.y},{slot.y + slot.h}]"
        )
        assert x >= screen[0] - _EDGE_TOL and y >= screen[1] - _EDGE_TOL, "off-screen"

    leftovers = _linux_kill_and_verify([title])
    assert not leftovers, f"cleanup left real windows/processes: {leftovers}"
    linux_cleanup.clear()


@pytest.mark.parametrize("emulator", _MATRIX)
def test_go_renders_and_selects_emulator_linux(
    emulator, tmp_path, linux_cleanup, capsys
):
    """Chain-tail render + selection: with ONLY ``emulator`` (xterm) discoverable
    on the launch PATH — every higher-priority chain entry asserted absent — real
    ``magent --go`` falls the ``shutil.which`` chain through to it and opens a
    real window owned by it, correctly titled (parsed by ``titles.parse_title``),
    with the shim command running inside, tiled into its ``compute_grid`` cell."""
    monitors, slots = _linux_ready()
    if not shutil.which(emulator):
        pytest.skip(f"{emulator} not installed on this runner")
    _launch_and_assert_owner(
        tmp_path,
        capsys,
        linux_cleanup,
        on_path=[emulator],
        winner=emulator,
        assert_geometry=True,
        monitors=monitors,
        slots=slots,
    )


# ===========================================================================
# Windows leg — no-wt reality: wt is a hard dependency, no console fallback
# ===========================================================================

_WM_CLOSE = 0x0010


def _enum_titles() -> list[str]:
    """Every visible top-level window title, via the product's own EnumWindows
    resolver (platform.snapshot_windows)."""
    return [t for t in get_platform().snapshot_windows() if isinstance(t, str)]


def _no_wt_path() -> str:
    """A PATH with wt made unresolvable: drop the WindowsApps execution-alias
    dir and any dir where wt still resolves, keeping every system dir (System32
    etc.) so magent itself runs normally."""
    kept = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and "windowsapps" not in d.lower()
    ]
    child = os.pathsep.join(kept)
    while (resolved := shutil.which("wt", path=child)) is not None:
        bad = os.path.normcase(os.path.dirname(resolved))
        kept = [d for d in kept if os.path.normcase(d) != bad]
        child = os.pathsep.join(kept)
    return child


def _win_child_env(home: Path, shim_dir: Path, base_path: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    env["PATH"] = str(shim_dir) + os.pathsep + base_path
    home_s = str(home)
    env["HOME"] = home_s
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    return env


def _close_md_windows(name: str) -> None:
    """Belt-and-braces: WM_CLOSE any magent:<name> window. None is expected (the
    no-wt path creates none), but if a future fallback ever does, never leak
    it."""
    snap = get_platform().snapshot_windows()
    for title, hwnd in snap.items():
        if isinstance(title, str) and f"magent:{name}" in title:
            ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows no-wt leg is win32-only")
def test_go_no_wt_creates_no_window_win(tmp_path, capsys):
    """PIN the wt hard-dependency + its clean-failure contract (TF-W-001): with
    wt removed from the child PATH, real ``magent --go`` reaches the launch
    phase then aborts with a CLEAN, ACTIONABLE error — non-zero exit, the
    ``winget install Microsoft.WindowsTerminal`` install hint on stderr/stdout,
    and NO raw traceback — creating NO magent: window and never running the tool
    shim. There is still no console fallback (magent fails fast with guidance,
    by design); this pins that reality until one is deliberately added."""
    from magent.platform import get_platform as _gp

    monitors = _gp().list_monitors()
    if not monitors:
        pytest.skip("no monitors detected on this Windows runner")

    unique = uuid.uuid4().hex[:10]
    name = f"mdnowt{unique}"
    project = tmp_path / f"proj-{unique}"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shim_dir = tmp_path / f"bin-{unique}"
    shim_dir.mkdir()
    marker = tmp_path / f"marker-{unique}.txt"
    # A benign claude shim as a safety net (must never run; asserted below).
    (shim_dir / "claude.cmd").write_text(
        f'@echo off\r\n>>"{marker}" echo claude#%*\r\n', encoding="utf-8"
    )

    base_path = _no_wt_path()
    env = _win_child_env(home, shim_dir, base_path)

    # Pre-flight rails: wt is genuinely unresolvable on the child PATH, and the
    # benign shim wins (so a stray real claude can never be invoked).
    assert shutil.which("wt", path=env["PATH"]) is None, (
        "wt must be unresolvable on the child PATH for the no-wt leg"
    )
    claude = shutil.which("claude", path=env["PATH"])
    assert claude is not None and str(shim_dir).lower() in claude.lower(), (
        f"benign claude shim must win PATH resolution, resolved to {claude!r}"
    )

    cfg = _write_single_window_config(tmp_path, name, project, "claude --continue")

    try:
        before = [t for t in _enum_titles() if f"magent:{name}" in t]
        assert not before, f"a stale magent:{name} window already exists: {before}"

        rc, out, err = _run_go(cfg, env, tmp_path)

        # 1. The launch failed (non-zero) with a CLEAN, actionable message --
        #    the winget install hint -- and NOT a raw FileNotFoundError
        #    traceback. The hint may land on stderr or stdout; accept either.
        assert rc != 0, f"--go unexpectedly succeeded without wt\nstdout:\n{out}"
        combined = out + err
        assert "winget install Microsoft.WindowsTerminal" in combined, (
            f"expected the actionable wt-install hint; stdout:\n{out}\nstderr:\n{err}"
        )
        assert "Traceback (most recent call last)" not in err, (
            f"a raw traceback leaked to stderr instead of a clean error; stderr:\n{err}"
        )

        # 2. No magent: window was ever created (poll a beat to be sure none appears).
        appeared = _wait_until(
            lambda: [t for t in _enum_titles() if f"magent:{name}" in t], timeout=10
        )
        assert not appeared, (
            f"no console fallback expected, but a window appeared: {appeared}"
        )

        # 3. The tool shim never ran — nothing was launched at all.
        assert not marker.exists(), (
            f"the tool command ran despite no terminal: {marker.read_text()!r}"
        )
        _real_stdout(
            capsys,
            f"no-wt: rc={rc} (wt hard-dependency confirmed; no console fallback)",
        )
    finally:
        _close_md_windows(name)
