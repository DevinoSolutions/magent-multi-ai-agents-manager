"""REAL psmux lifecycle + full attach chain: sessions brought up through the
same platform primitives the launch/attach paths use, interrogated and torn
down through the psmux module's real subprocess primitives.

What this proves (against a live psmux server, zero stubs):

* ``platform.launch_psmux_session`` really creates a detached session
  (``has_session`` true) rooted at the requested cwd;
* ``psmux.pane_cwd`` (the #41 pane_cwd guard's happy path) reports that REAL
  working directory back from the live pane;
* after ``kill_server``, ``pane_cwd`` degrades to ``""`` promptly (well inside
  its 3s subprocess-timeout guard) and ``has_session`` is false -- the exact
  degradation the guard promises callers that fan this across sessions.

Skips cleanly when psmux is not installed or the platform has no psmux
support (macOS/Linux, and CI runners without the binary).
"""

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from magent import psmux
from magent.platform import PsmuxWindowOpts, get_platform

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(psmux.find_psmux() is None, reason="psmux not installed"),
    pytest.mark.skipif(
        not get_platform().supports_psmux(),
        reason="platform backend has no psmux session support",
    ),
]


def _wait_until(check, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _norm_path(p: str) -> str:
    """Tolerant path normalizer for comparing psmux's pane_current_path (which
    may come back cygwin-style, e.g. /c/Users/... or /cygdrive/c/...) against a
    Windows path -- forward slashes, drive letter unified, casefolded."""
    s = p.strip().replace("\\", "/")
    for prefix in ("/cygdrive/", "/"):
        rest = s[len(prefix) :]
        if (
            s.startswith(prefix)
            and len(rest) >= 2
            and rest[1] == "/"
            and rest[0].isalpha()
        ):
            s = f"{rest[0]}:{rest[1:]}"
            break
    return s.rstrip("/").casefold()


def _same_dir(reported: str, expected: Path) -> bool:
    if not reported:
        return False
    try:
        if os.path.exists(reported) and os.path.samefile(reported, expected):
            return True
    except OSError:
        pass
    return _norm_path(reported) == _norm_path(str(expected))


def test_real_session_pane_cwd_and_kill(tmp_path):
    unique = uuid.uuid4().hex[:12]
    name = f"mdrl-psx-{unique}"
    workdir = tmp_path / f"cwd-{unique}"
    workdir.mkdir()

    created = False
    try:
        # Bring the session up the way run_magent does: through the
        # platform primitive (real `psmux new-session -d -c <cwd>` + send-keys).
        get_platform().launch_psmux_session(
            [
                PsmuxWindowOpts(
                    window_name=name,
                    cwd=str(workdir),
                    command=f"rem mdrl-{unique}",
                )
            ]
        )
        created = True

        assert _wait_until(lambda: psmux.has_session(name), timeout=10), (
            f"psmux session {name!r} never came up"
        )

        reported = _wait_until(lambda: psmux.pane_cwd(name), timeout=15)
        assert _same_dir(reported, workdir), (
            f"pane_cwd reported {reported!r}, expected {workdir}"
        )

        # Kill THIS session's server and watch the primitives degrade honestly.
        assert psmux.kill_server(name), f"kill_server({name!r}) failed"

        assert _wait_until(
            lambda: not psmux.has_session(name) and psmux.pane_cwd(name) == "",
            timeout=3.5,
        ), "session still answering ~3s after kill_server"

        # The pane_cwd timeout guard, for real: a call against the dead server
        # returns "" and does so promptly (bounded by its own 3s guard).
        start = time.monotonic()
        assert psmux.pane_cwd(name) == ""
        assert time.monotonic() - start < 4.0, "pane_cwd exceeded its timeout guard"
    finally:
        if created:
            psmux.kill_server(name)  # idempotent; only ever targets our name

    assert not psmux.has_session(name), f"cleanup left psmux session {name!r} alive"


# --- full chain: create -> attach in a REAL wt window -> teardown -------------
#
# What the chain test proves beyond the lifecycle test above (still zero
# stubs): the ATTACH path. ``platform.attach_psmux`` opens a real Windows
# Terminal window running ``psmux attach`` against the live session, titled by
# the product's own ``titles.make_title`` grammar; the window materializes on
# the real desktop under exactly that ``magent:`` title; the psmux server sees a
# REAL attached client (``list-clients``); and after ``kill_server`` the
# session is gone and the primitives degrade as promised. Cleanup closes
# exactly the one uuid-titled window this test opened.

_WM_CLOSE = 0x0010


def _list_clients(name: str) -> str:
    """Raw ``psmux list-clients`` output for a session ("" on error)."""
    import subprocess

    binary = psmux.find_psmux()
    assert binary is not None  # module pytestmark guarantees it
    try:
        result = subprocess.run(
            [binary, "-L", name, "list-clients"],
            capture_output=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


@pytest.mark.skipif(
    shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
)
def test_full_chain_create_attach_in_real_wt_window_teardown(tmp_path):
    import ctypes

    from magent.platform import get_platform
    from magent.titles import make_title, parse_title

    plat = get_platform()
    unique = uuid.uuid4().hex[:12]
    name = f"mdrl-att-{unique}"
    title = make_title(name)  # the product's title grammar, never hand-built
    workdir = tmp_path / f"cwd-{unique}"
    workdir.mkdir()

    created = False
    try:
        # 1. Create the detached session through the launch-path primitive.
        get_platform().launch_psmux_session(
            [
                PsmuxWindowOpts(
                    window_name=name,
                    cwd=str(workdir),
                    command=f"rem mdrl-att-{unique}",
                )
            ]
        )
        created = True
        assert _wait_until(lambda: psmux.has_session(name), timeout=10), (
            f"psmux session {name!r} never came up"
        )
        # Empirical psmux quirk (pinned): a fresh DETACHED session already
        # reports one pseudo-client (e.g. "/dev/pts/0: ... pwsh"), so "no
        # clients before attach" is false. The attach proof below is therefore
        # a DELTA: the wt attach must add a client beyond this baseline.
        baseline = {ln for ln in _list_clients(name).splitlines() if ln}

        # 2. Attach through the product attach path: a REAL wt window.
        plat.attach_psmux(name, title)

        hwnd = _wait_until(lambda: plat.find_window(title), timeout=90)
        assert hwnd, (
            f"attach window {title!r} never materialized; magent: windows visible: "
            f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}"
        )
        # The title on the live HWND round-trips through the product grammar.
        parsed = parse_title(title)
        assert parsed == (name, None)

        # 3. The psmux server sees a REAL new attached client.
        def _new_clients() -> set[str]:
            return {ln for ln in _list_clients(name).splitlines() if ln} - baseline

        clients = _wait_until(_new_clients, timeout=30)
        assert clients, (
            f"no NEW client attached to {name!r} after the wt window opened; "
            f"baseline={sorted(baseline)}, now={_list_clients(name)!r}"
        )

        # 4. Teardown: detach the client through the product primitive, then
        #    kill the server and watch the primitives degrade honestly.
        assert psmux.detach_client(name), "detach_client failed against a live client"
        assert psmux.kill_server(name), f"kill_server({name!r}) failed"
        assert _wait_until(lambda: not psmux.has_session(name), timeout=5), (
            "session still answering after kill_server"
        )
    finally:
        if created:
            psmux.kill_server(name)
        # Close exactly our uuid-titled window (the attach client may keep the
        # tab open after the server dies; wt closeOnExit behavior is not ours
        # to assert). Verified gone below.
        hwnd = plat.find_window(title)
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        _wait_until(lambda: plat.find_window(title) is None, timeout=15)

    assert not psmux.has_session(name), f"cleanup left psmux session {name!r} alive"
    assert plat.find_window(title) is None, f"cleanup left window {title!r} open"
