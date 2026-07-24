"""REAL end-to-end launch on POSIX: ``python -m magent --go`` as a subprocess
opening two actual terminal windows, tiled by the real platform pipeline -- the
Linux/macOS sibling of ``test_real_launch.py`` (the win32 tier, PR #45).

What this proves about the user experience (no fakes, no dry-run, no
monkeypatching):

* a v3 config with two ``windows`` entries launches one real terminal per
  window, each titled exactly ``magent:<name>``;
* magent's own launch pipeline -- ``LinuxPlatform.snapshot_windows`` +
  ``move_window`` (wmctrl) -- finds those windows by title and moves them into
  the grid cells ``grid.compute_grid`` derives from the real monitor set;
* the real windows land inside the screen bounds, in their computed cells,
  without overlap.

Two legs, calibrated to what each CI runner can honestly deliver:

* **Linux (fully real).** Xvfb + a window manager (openbox, started by
  ``.github/actions/setup-virtual-displays``) host real ``xterm`` windows. A WM
  is mandatory: without one wmctrl's move/resize silently no-ops. Every
  assertion is hard.
* **macOS (best-effort).** System Events / Terminal AppleScript are TCC-gated
  on hosted runners and often blocked (no one can click the consent prompt). A
  preflight probe decides: real assertions when automation is permitted, else a
  loud GitHub ``::warning`` + skip. TCC blockage never masquerades as a green
  "tested" leg, and Apple's UI-automation restrictions never fail the job.

Isolation: the child process gets a redirected ``HOME`` (its ``~/.magent``
logs and ``~/.claude`` session scans land under tmp_path, never the real user's)
and no ambient ``MAGENT_*`` vars; ``DISPLAY`` is inherited. The configured
tool is a benign ``sleep`` -- real terminals, real command execution, zero cost,
NEVER a real ``claude``/``codex``. Cleanup closes exactly the uuid-titled
windows (and kills the child pids they own) and verifies none survive, failing
loudly on any leftover.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.platform

# Per-edge slack: WM decorations (openbox titlebar/border), terminal
# character-cell rounding (xterm snaps to font metrics), and the frame-vs-client
# gravity ambiguity in how a WM honours a move+resize request. Generous, but far
# below a cell width (~2880px on the 5760px Xvfb screen), so it still pins each
# window to the correct cell.
_EDGE_TOL = 160
_OVERLAP_TOL = 64  # px: max tolerated intrusion into the neighbour cell


@dataclass
class _Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def _child_env(home) -> dict[str, str]:
    """Env for the child: the real user env, but HOME redirected under tmp_path
    (so its ~/.magent logs and ~/.claude session scan never touch the real
    user's) and every MAGENT_* var stripped. DISPLAY is left intact so the
    child talks to the same X server this test polls."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    env["HOME"] = home_s
    # Point XDG dirs into the sandbox home too, so a stray ~/.config write can't
    # escape into the real user's dotfiles.
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    return env


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
    """Write a line to the *real* step stdout with pytest capture suspended
    (sys.stdout.write, not print(), which T20 bans in tests). Used both for
    GitHub ``::warning`` annotations and for auditable geometry diagnostics --
    captured output would never reach the CI log's annotation parser."""
    with capsys.disabled():
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_ci_warning(capsys, title: str, message: str) -> None:
    """Emit a GitHub Actions ``::warning`` annotation, loudly visible in the CI
    log so a best-effort skip never hides inside captured output."""
    _real_stdout(capsys, f"::warning title={title}::{message}")


def _write_two_window_config(tmp_path, unique: str) -> tuple[str, list[str], object]:
    """Write a v3, 2-window config driving a benign ``sleep`` tool. Returns
    ``(config_path, [title_a, title_b], home_dir)``."""
    name_a = f"mdrl{unique}a"
    name_b = f"mdrl{unique}b"
    from magent.titles import make_title

    title_a, title_b = make_title(name_a), make_title(name_b)

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "defaultTool": "sleeper",
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    # Benign, long-lived, NEVER a real agent. The window stays
                    # open until cleanup kills it (or 5 min, whichever first).
                    "tools": {"sleeper": "sleep 300"},
                },
                "projects": [
                    {
                        "path": str(proj),
                        "title": f"mdrl{unique}",
                        "windows": [{"name": name_a}, {"name": name_b}],
                    }
                ],
            }
        )
    )
    return str(cfg), [title_a, title_b], home


def _run_go(cfg: str, home, tmp_path, timeout: float = 90) -> tuple[int, str, str]:
    """Run ``magent --go`` to completion, capturing output via FILES, not a
    pipe. Critical: the launched terminal (xterm/Terminal) inherits the child's
    stdout/stderr and holds it open for the life of its ``sleep`` -- a captured
    PIPE keeps ``subprocess.run`` blocked on EOF for the whole sleep (a 120s
    hang, observed on both Linux and macOS). Redirecting to files makes run()
    wait only for the magent process itself to exit. Returns
    ``(returncode, stdout, stderr)``."""
    out_path = tmp_path / "go.stdout"
    err_path = tmp_path / "go.stderr"
    with (
        out_path.open("w", encoding="utf-8") as fo,
        err_path.open("w", encoding="utf-8") as fe,
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "magent", "--go", "--config", cfg],
            stdout=fo,
            stderr=fe,
            timeout=timeout,
            env=_child_env(home),
        )
    return (
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def _assert_in_cell(rect: _Rect, slot, screen: _Rect, label: str) -> None:
    """The window occupies its computed cell (edges within tolerance + centre
    inside the cell) and stays on-screen. Mirrors the win32 tier's edge +
    centre-in-cell checks, widened for POSIX WM decorations."""
    assert abs(rect.left - slot.x) <= _EDGE_TOL, (
        f"{label}: left {rect.left} vs slot {slot.x}"
    )
    assert abs(rect.top - slot.y) <= _EDGE_TOL, (
        f"{label}: top {rect.top} vs slot {slot.y}"
    )
    assert abs(rect.right - (slot.x + slot.w)) <= _EDGE_TOL, (
        f"{label}: right {rect.right} vs slot {slot.x + slot.w}"
    )
    assert abs(rect.bottom - (slot.y + slot.h)) <= _EDGE_TOL, (
        f"{label}: bottom {rect.bottom} vs slot {slot.y + slot.h}"
    )
    assert slot.x <= rect.center_x <= slot.x + slot.w, (
        f"{label}: center_x {rect.center_x} outside its cell [{slot.x},{slot.x + slot.w}]"
    )
    assert slot.y <= rect.center_y <= slot.y + slot.h, (
        f"{label}: center_y {rect.center_y} outside its cell [{slot.y},{slot.y + slot.h}]"
    )
    assert rect.left >= screen.left - _EDGE_TOL, f"{label} off-screen left"
    assert rect.top >= screen.top - _EDGE_TOL, f"{label} off-screen top"
    assert rect.right <= screen.right + _EDGE_TOL, f"{label} off-screen right"
    assert rect.bottom <= screen.bottom + _EDGE_TOL, f"{label} off-screen bottom"


# ===========================================================================
# Linux leg -- fully real (Xvfb + window manager)
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


def _screen_bounds(monitors) -> _Rect:
    """Union of the monitor rectangles -- the region compute_grid tiles into,
    and the correct on-screen envelope for the tiled windows. NB: on Xvfb,
    ``xdotool getdisplaygeometry`` reports the primary RANDR monitor (e.g.
    1920x1080), not the full 5760x2160 root, so it under-reports a multi-cell
    virtual screen and must not be used as the bound."""
    return _Rect(
        min(m.x for m in monitors),
        min(m.y for m in monitors),
        max(m.x + m.w for m in monitors),
        max(m.y + m.h for m in monitors),
    )


def _client_rect(wid: str) -> _Rect:
    """The client window's on-screen rect (root coordinates) via xdotool."""
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
    return _Rect(
        vals["X"], vals["Y"], vals["X"] + vals["WIDTH"], vals["Y"] + vals["HEIGHT"]
    )


def _frame_extents(wid: str) -> tuple[int, int, int, int]:
    """(left, right, top, bottom) WM decoration widths via _NET_FRAME_EXTENTS,
    (0,0,0,0) when unavailable -- purely diagnostic slack accounting."""
    if not shutil.which("xprop"):
        return (0, 0, 0, 0)
    r = subprocess.run(
        ["xprop", "-id", wid, "_NET_FRAME_EXTENTS"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    m = re.search(r"=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", r.stdout)
    if not m:
        return (0, 0, 0, 0)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4]))


def _linux_kill_and_verify(plat, titles: list[str]) -> list[str]:
    """Kill exactly the windows carrying ``titles`` (and the xterm pids that own
    them, which cascades SIGHUP to their ``sleep`` children through the closing
    pty) and return whatever still answers to those titles. Never touches a
    window this test did not create -- the titles are uuid-namespaced."""
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

    # Last-resort SIGKILL for any surviving owner, then re-verify.
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
    """Teardown-as-safety-net: whatever the test registers is closed and
    verified gone even if the body fails, so a broken assertion never leaks a
    real xterm. A failed cleanup is a loud teardown error."""
    from magent.platform import get_platform

    titles: list[str] = []
    yield titles
    if titles:
        leftovers = _linux_kill_and_verify(get_platform(), titles)
        assert not leftovers, f"cleanup left real windows/processes: {leftovers}"


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux real-render leg is linux-only"
)
def test_go_launches_real_tiled_xterms_linux(tmp_path, linux_cleanup, capsys):
    if not os.environ.get("DISPLAY"):
        pytest.skip("DISPLAY not set: no X server to host real windows")
    for tool in ("xterm", "xdotool", "wmctrl"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not installed: required for the real xterm render leg")
    # A window manager is mandatory -- without one wmctrl's move/resize no-ops,
    # so tiling could not move anything and the assertion would be meaningless.
    wm = subprocess.run(["wmctrl", "-m"], capture_output=True, text=True, check=False)
    if wm.returncode != 0:
        pytest.skip(
            "no EWMH window manager on DISPLAY: wmctrl move/resize would no-op "
            "(start one in setup-virtual-displays)"
        )

    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    monitors = plat.list_monitors()
    assert monitors, "no monitors detected on the X display"
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("display cannot host a 2x1 grid (DPI floor collapsed it)")

    unique = uuid.uuid4().hex[:10]
    cfg, titles, home = _write_two_window_config(tmp_path, unique)
    title_a, title_b = titles
    linux_cleanup.extend(titles)

    rc, out, err = _run_go(cfg, home, tmp_path)
    assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"
    assert "not found" not in out, (
        f"tiling gave up on a window (snapshot_windows/move_window):\n{out}"
    )

    # 1. Both REAL windows exist with the exact magent: titles -- belt and braces:
    #    the platform's own find_window AND raw xdotool must agree.
    both = _wait_until(
        lambda: bool(_xdotool_ids(title_a)) and bool(_xdotool_ids(title_b)),
        timeout=20,
    )
    visible = [
        t
        for t in plat.snapshot_windows()
        if isinstance(t, str) and t.startswith("magent:")
    ]
    assert both, (
        f"expected xterms {title_a!r} and {title_b!r}; magent: windows seen: {visible}"
    )
    assert plat.find_window(title_a) is not None, f"find_window missed {title_a!r}"
    assert plat.find_window(title_b) is not None, f"find_window missed {title_b!r}"

    # 2. On-screen geometry lands in the computed cells. Log the raw readback +
    #    frame extents so the tolerances are auditable from the CI log.
    screen = _screen_bounds(monitors)
    id_a, id_b = _xdotool_ids(title_a)[0], _xdotool_ids(title_b)[0]
    rect_a, rect_b = _client_rect(id_a), _client_rect(id_b)
    _real_stdout(capsys, f"screen={screen} frame_extents_a={_frame_extents(id_a)}")
    _real_stdout(capsys, f"A slot={slots[0]} rect={rect_a}")
    _real_stdout(capsys, f"B slot={slots[1]} rect={rect_b}")
    _assert_in_cell(rect_a, slots[0], screen, "window A")
    _assert_in_cell(rect_b, slots[1], screen, "window B")

    # 3. Ordered per the grid and non-overlapping when on one monitor.
    if slots[0].monitor_index == slots[1].monitor_index:
        assert slots[0].x < slots[1].x  # compute_grid emits left-to-right
        assert rect_a.center_x < rect_b.center_x, "grid order violated on screen"
        assert rect_a.right - rect_b.left <= _OVERLAP_TOL, (
            f"windows overlap: A.right={rect_a.right} B.left={rect_b.left}"
        )

    # 4. Cleanup kills exactly those two windows; verify gone (fail loudly).
    leftovers = _linux_kill_and_verify(plat, titles)
    assert not leftovers, f"cleanup left real windows/processes: {leftovers}"
    linux_cleanup.clear()
    assert plat.find_window(title_a) is None
    assert plat.find_window(title_b) is None


# ===========================================================================
# macOS leg -- best-effort, gated on a TCC automation preflight probe
# ===========================================================================


def _system_events_ok() -> bool:
    """Cheap preflight: can this process script System Events at all?"""
    out = _osascript('tell application "System Events" to count processes', 15)
    return out is not None and out.strip().isdigit()


def _osascript(script: str, timeout: float) -> str | None:
    """Run an osascript snippet; return stdout, or None if it errored OR HUNG
    past ``timeout``. A blocked TCC consent prompt manifests as a hang (osascript
    never returns) -- catching TimeoutExpired here is what keeps every macOS-leg
    Apple-event call from surfacing as an uncaught failure instead of a clean,
    honest skip. Every osascript on this leg goes through here."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    return r.stdout if r.returncode == 0 else None


def _terminal_scriptable() -> bool:
    """Can this process drive Terminal.app via Apple events? A SEPARATE TCC
    bucket from System Events -- launch uses ``tell application "Terminal" to do
    script``. A blocked/hung consent prompt returns None here (bounded), so we
    fail fast instead of sinking ~a minute into a --go that launches nothing."""
    out = _osascript('tell application "Terminal" to count windows', 20)
    return out is not None and out.strip().isdigit()


def _macos_geometry(proc: str, win: str) -> _Rect | None:
    script = (
        f'tell application "System Events" to tell process "{proc}"\n'
        f'set p to position of window "{win}"\n'
        f'set s to size of window "{win}"\n'
        f"end tell\n"
        f'return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," '
        f'& ((item 1 of s) as text) & "," & ((item 2 of s) as text)'
    )
    out = _osascript(script, 10)
    if out is None:
        return None
    m = re.match(r"\s*(-?\d+),(-?\d+),(-?\d+),(-?\d+)", out.strip())
    if not m:
        return None
    x, y, w, h = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    return _Rect(x, y, x + w, y + h)


def _macos_close(plat, titles: list[str]) -> list[str]:
    """Best-effort close of exactly our Terminal windows: kill each window's tty
    process group (the shell + its ``sleep`` child), then close the window with
    no save/quit prompt. Returns titles still visible afterward."""
    for title in titles:
        script = (
            f'tell application "Terminal"\n'
            f"repeat with w in windows\n"
            f"try\n"
            f'if (custom title of w) is "{title}" then\n'
            f"set t to tty of (selected tab of w)\n"
            f'do shell script "pkill -t " & (last item of (my splitTty(t)))\n'
            f"close w saving no\n"
            f"end if\n"
            f"end try\n"
            f"end repeat\n"
            f"end tell\n"
            f"on splitTty(t)\n"
            f'set AppleScript\'s text item delimiters to "/"\n'
            f"return text items of t\n"
            f"end splitTty"
        )
        _osascript(script, 15)  # best-effort; None (hang/error) is fine here
    snap = plat.snapshot_windows()
    return [t for t in titles if t in snap]


@pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS real-render leg is macos-only"
)
def test_go_launches_real_tiled_terminals_macos(tmp_path, capsys):
    if not shutil.which("osascript"):
        pytest.skip("osascript not available")
    if not _system_events_ok():
        _emit_ci_warning(
            capsys,
            "macOS UI automation blocked (TCC)",
            "System Events scripting is not permitted for this process on the "
            "hosted runner; the macOS real-render leg cannot inspect windows and "
            "is skipped (not a green pass).",
        )
        pytest.skip("System Events automation blocked (TCC): real render unattainable")
    if not _terminal_scriptable():
        _emit_ci_warning(
            capsys,
            "macOS Terminal automation blocked (TCC)",
            "Terminal.app 'do script' automation is not permitted (a separate TCC "
            "bucket from System Events); magent cannot launch windows here, so "
            "the macOS real-render leg is skipped (not a green pass).",
        )
        pytest.skip("Terminal automation blocked (TCC): cannot launch windows")

    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    monitors = plat.list_monitors()
    if not monitors:
        pytest.skip("no monitors detected on this macOS runner")
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("display cannot host a 2x1 grid (DPI floor collapsed it)")
    screen = _screen_bounds(monitors)

    unique = uuid.uuid4().hex[:10]
    cfg, titles, home = _write_two_window_config(tmp_path, unique)
    title_a, title_b = titles

    try:
        try:
            rc, _out, err = _run_go(cfg, home, tmp_path, timeout=75)
        except subprocess.TimeoutExpired:
            _emit_ci_warning(
                capsys,
                "macOS real render unattainable",
                "magent --go did not complete in time on this runner "
                "(Terminal/System Events UI automation too slow or blocked); "
                "skipping the macOS geometry assertion (not a green pass).",
            )
            pytest.skip("--go did not complete (UI automation slow/blocked)")
        if rc != 0:
            _emit_ci_warning(
                capsys,
                "macOS real render unattainable",
                f"magent --go exited {rc} on this runner; skipping the macOS "
                f"geometry assertion (not a green pass).\nstderr:\n{err}",
            )
            pytest.skip(f"--go exited {rc} (UI automation restricted)")

        # Real render is attainable only if Terminal actually materialised both
        # windows. If not, that is Apple blocking UI automation -- loud skip.
        both = _wait_until(
            lambda: all(t in plat.snapshot_windows() for t in titles), timeout=20
        )
        if not both:
            seen = [
                t
                for t in plat.snapshot_windows()
                if isinstance(t, str) and t.startswith("magent:")
            ]
            _emit_ci_warning(
                capsys,
                "macOS real render unattainable",
                "Terminal did not yield both magent: windows on this runner "
                f"(TCC-restricted UI automation); magent: windows seen: {seen}.",
            )
            pytest.skip("Terminal did not materialise the windows (TCC-restricted)")

        # Both windows exist -> real geometry assertions.
        snap = plat.snapshot_windows()
        for label, title, slot in (
            ("window A", title_a, slots[0]),
            ("window B", title_b, slots[1]),
        ):
            handle = snap[title]
            assert isinstance(handle, dict)
            geom = _macos_geometry(str(handle["process"]), str(handle["window"]))
            if geom is None:
                _emit_ci_warning(
                    capsys,
                    "macOS geometry readback blocked",
                    "System Events returned no geometry for a launched window "
                    "(partial TCC restriction); skipping the geometry assertion.",
                )
                pytest.skip("System Events geometry readback unavailable (TCC)")
            _real_stdout(capsys, f"{label} slot={slot} rect={geom}")
            _assert_in_cell(geom, slot, screen, label)
    finally:
        # Best-effort on an ephemeral runner: closing Terminal windows is itself
        # TCC-gated, so a leftover is warned (loudly), never a job failure -- an
        # assert here would convert an honest skip into a fail. The Linux leg,
        # which owns its Xvfb windows outright, hard-asserts cleanup instead.
        leftovers = _macos_close(plat, titles)
        if leftovers:
            _emit_ci_warning(
                capsys,
                "macOS cleanup leftover",
                f"could not close real Terminal windows on this ephemeral runner "
                f"(TCC-restricted): {leftovers}",
            )
