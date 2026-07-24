"""REAL SSH tier: every test in this file traverses a live loopback sshd.

The pre-existing ``test_ssh.py`` only ever runs ``--go --dry-run`` -- it pins
the config/warning surface but never opens a connection, so the product's SSH
surface (launch.py's nested remote quoting, cli/attach.py's whole
attach-over-SSH flow) was untested over a real wire. CI provisions a real
OpenSSH server on all three OSes (``.github/actions/setup-ssh-server``: keys,
an ``mdssh`` host alias in the REAL ``~/.ssh/config``, and -- exported for
these tests -- ``MDTEST_SSH_PORT`` / ``MDTEST_SSH_KEY`` / ``MDTEST_SSH_HOST``).
These tests use it. No fakes, no dry-run, no monkeypatched transport:

* ``test_up_json_round_trips_over_live_sshd`` (all OSes) -- the exact
  non-interactive command shape ``magent attach`` sends
  (``_ssh_capture``/``_ssh_json``: ``ssh -o BatchMode=yes <target> "magent
  ... up --json"``) round-trips through sshd: key auth, the sshd session's
  PATH, remote config load, and the one-line JSON envelope all proven live.
* ``test_attach_over_real_ssh_windows`` (win32 flagship) -- the product's
  headline remote workflow end to end against localhost-as-remote: seed the
  remote HOME with a uuid-namespaced config, run ``magent attach mdssh -y``,
  and assert psmux sessions were created by the REMOTE bring-up and survive
  the ssh session closing, real ``wt`` windows open running
  ``ssh -t ... magent sessions <sid>`` with exact ``magent:`` titles, tiling
  places them into their computed cells, and ``serve --ensure`` (sent over
  ssh) leaves a live upload server answering ``/health`` after its ssh
  session is gone (the spawn_detached job-object-breakaway contract).
* ``test_go_remote_launch_marker_over_live_sshd_linux`` -- a real ``--go``
  (no dry-run) drives launch.py's ssh branch: ``xterm -e "cd <cwd> && ssh -t
  mdssh \"bash -lc 'cd <dir> && touch <marker> && sleep 300'\""``. The marker
  file appearing proves the full nested-quoting chain executed on the far
  side of a real connection.
* macOS window legs are a LOUD skip (``::warning``), mirroring the
  tests/platform PR-#47 precedent: Terminal automation is TCC-blocked on
  hosted runners, and the windowless wire coverage above still runs there.

Safety rails honored here: the remote side of ``attach`` reads the ssh user's
real ``HOME`` (attach cannot inject ``--config`` into the commands it sends),
so every test that seeds or mutates the real HOME is gated behind
``GITHUB_ACTIONS=true`` (ephemeral CI VM) or an explicit
``MDTEST_ALLOW_REAL_HOME=1`` opt-in, and skips loudly otherwise. Locally the
``MDTEST_SSH_*`` variables are simply absent and everything here skips --
never install an SSH server on a developer machine for this file. Tool
commands are echo/touch/sleep markers, NEVER a real agent; all artifacts are
uuid-namespaced; cleanup kills exactly the sessions/windows/pids this file
created and verifies them gone.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.needs_ssh]

_WM_CLOSE = 0x0010


# ---------------------------------------------------------------------------
# Shared gates and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def ssh_wire():
    """The CI-provisioned loopback sshd, or a clean skip when absent (local)."""
    port = os.environ.get("MDTEST_SSH_PORT")
    key = os.environ.get("MDTEST_SSH_KEY")
    host = os.environ.get("MDTEST_SSH_HOST")
    if not port or not key or not host:
        pytest.skip(
            "live SSH server not configured (setup-ssh-server exports "
            "MDTEST_SSH_PORT/MDTEST_SSH_KEY/MDTEST_SSH_HOST in CI; local runs skip)"
        )
    if shutil.which("ssh") is None:
        pytest.skip("ssh client not on PATH")
    return {"port": port, "key": key, "host": host}


def _require_real_home_ok() -> None:
    """Hard gate for tests that write the ssh user's REAL home: ephemeral CI
    VMs only (or an explicit opt-in), never a developer machine."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return
    if os.environ.get("MDTEST_ALLOW_REAL_HOME") == "1":
        return
    pytest.skip(
        "test seeds the ssh user's real HOME (attach cannot inject --config "
        "remotely); allowed only on CI VMs (GITHUB_ACTIONS=true) or with an "
        "explicit MDTEST_ALLOW_REAL_HOME=1 opt-in"
    )


def _wait_until(check, timeout: float, interval: float = 0.25):
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
    import http.client

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200 and json.loads(resp.read()).get("ok") is True
        finally:
            conn.close()
    except (OSError, ValueError):
        return False


def _run_to_files(
    args: list[str],
    tmp_path,
    tag: str,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run a child to completion, capturing output via FILES, never a pipe.

    Launched terminals (wt/xterm) and detached survivors (upload server,
    hotkey listener) inherit the child's stdout/stderr and hold a PIPE open
    long after the CLI itself exits -- a captured PIPE keeps subprocess.run
    blocked on EOF for the survivor's whole lifetime (deadlocked PR #47's
    first run). Files make run() wait only for the CLI process."""
    out_path = tmp_path / f"{tag}.stdout"
    err_path = tmp_path / f"{tag}.stderr"
    with (
        out_path.open("w", encoding="utf-8") as fo,
        err_path.open("w", encoding="utf-8") as fe,
    ):
        proc = subprocess.run(
            args, stdout=fo, stderr=fe, timeout=timeout, env=env, cwd=cwd
        )
    return (
        proc.returncode,
        out_path.read_text(encoding="utf-8", errors="replace"),
        err_path.read_text(encoding="utf-8", errors="replace"),
    )


def _real_stdout(capsys, line: str) -> None:
    """Write to the real step stdout with pytest capture suspended, so GitHub
    ``::warning`` annotations reach the CI log's parser."""
    with capsys.disabled():
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _emit_ci_warning(capsys, title: str, message: str) -> None:
    _real_stdout(capsys, f"::warning title={title}::{message}")


def _ssh_target(host: str) -> str:
    import getpass

    return f"{getpass.getuser()}@{host}"


def _quoted_config_arg(cfg: Path) -> str:
    """A --config path token safe inside the one remote command string.

    Both remote shells in play (cmd.exe on Windows sshd, ``$SHELL -c`` on
    POSIX) parse double quotes; the paths are spaceless on CI runners but
    quote anyway."""
    return f'"{cfg}"'


# ---------------------------------------------------------------------------
# 1. All OSes: the attach control channel round-trips over the live wire
# ---------------------------------------------------------------------------


class TestSshControlChannel:
    def test_up_json_round_trips_over_live_sshd(self, tmp_path, ssh_wire):
        """`magent ... up --json` -- the exact remote query `attach` opens
        with -- round-trips through the real sshd: key auth (BatchMode), the
        sshd session's PATH resolving the installed `magent`, remote config
        load, and the single-line JSON envelope parsed from mixed output."""
        from magent.cli.attach import _ssh_capture, _ssh_json

        unique = uuid.uuid4().hex[:8]
        name_a, name_b = f"mdsshq{unique}a", f"mdsshq{unique}b"
        proj_a = tmp_path / name_a
        proj_b = tmp_path / name_b
        proj_a.mkdir()
        proj_b.mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "projects": [
                        {"path": str(proj_a), "title": name_a},
                        {"path": str(proj_b), "title": name_b},
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "tools": {"probe": f"echo mdssh-wire-{unique}"},
                        "uploadServer": False,
                    },
                }
            )
        )

        target = _ssh_target(ssh_wire["host"])

        # Transport sanity first, with the full stderr surfaced on failure --
        # this is the line that catches a broken key/alias/PATH before the
        # JSON assertion can only say "None".
        rc, out, err = _ssh_capture(target, "magent --version", timeout=60)
        assert rc == 0, (
            f"`ssh {target} magent --version` failed over the live wire "
            f"(rc={rc})\nstdout:\n{out}\nstderr:\n{err}"
        )

        status = _ssh_json(
            target,
            f"magent --config {_quoted_config_arg(cfg)} up --json",
            timeout=60,
        )
        assert status is not None, f"no JSON envelope came back over ssh from {target}"
        assert status.get("ok") is True
        # Same machine on both ends of the wire -- the envelope must agree.
        assert status.get("platform") == sys.platform

        projects = status.get("projects")
        assert isinstance(projects, list)
        by_name = {p.get("name"): p for p in projects if isinstance(p, dict)}
        assert set(by_name) == {name_a, name_b}
        # Titles chosen with no dots/colons/spaces: session id == title.
        assert by_name[name_a].get("session") == name_a

        # Nothing was brought up: every session reports down, none up.
        down = status.get("down")
        assert isinstance(down, list)
        down_names = {d.get("session") for d in down if isinstance(d, dict)}
        assert {name_a, name_b} <= down_names
        assert status.get("up") == []


# ---------------------------------------------------------------------------
# 2. Windows flagship: `magent attach` against a real remote over real ssh
# ---------------------------------------------------------------------------


def _snapshot_titles(plat, titles: list[str]) -> dict[str, object]:
    snap = plat.snapshot_windows()
    return {t: snap[t] for t in titles if t in snap}


def _window_pid(hwnd) -> int | None:
    pid = ctypes.c_ulong()  # DWORD; ctypes.wintypes stays un-imported off-win32
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def _taskkill(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
    )


def _close_windows_and_verify_gone(plat, titles: list[str]) -> list[str]:
    """WM_CLOSE exactly the given windows (killing their process trees as a
    force-fallback: wt hosts the local `ssh -t`, whose death drops the remote
    session) and return whatever still answers to those titles."""
    for hwnd in _snapshot_titles(plat, titles).values():
        ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    _wait_until(lambda: not _snapshot_titles(plat, titles), timeout=15)

    for hwnd in _snapshot_titles(plat, titles).values():
        pid = _window_pid(hwnd)
        if pid:
            _taskkill(pid)
    _wait_until(lambda: not _snapshot_titles(plat, titles), timeout=10)

    return [f"window {t}" for t in _snapshot_titles(plat, titles)]


def _kill_upload_server(port: int) -> None:
    from magent.procs import pid_alive
    from magent.upload_server import server_pid

    pid = server_pid(port)
    if pid and pid_alive(pid):
        _taskkill(pid)
        _wait_until(lambda: not pid_alive(pid), timeout=10)
    with suppress(OSError):
        (Path.home() / ".magent" / f"upload_server-{port}.pid").unlink()


def _seed_files(seeds: list[tuple[Path, str]]) -> list[tuple[Path, bytes | None]]:
    """Write each (path, content), remembering what was there before so
    teardown can restore a dev machine byte-for-byte (CI VMs have nothing)."""
    memo: list[tuple[Path, bytes | None]] = []
    for path, content in seeds:
        prior = path.read_bytes() if path.exists() else None
        memo.append((path, prior))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return memo


def _restore_files(memo: list[tuple[Path, bytes | None]]) -> None:
    for path, prior in memo:
        if prior is None:
            with suppress(OSError):
                path.unlink()
        else:
            path.write_bytes(prior)


@pytest.mark.skipif(sys.platform != "win32", reason="attach opens wt windows: win32")
class TestAttachOverRealSsh:
    def test_attach_over_real_ssh_windows(self, tmp_path, ssh_wire):
        """The headline remote workflow, end to end over a live sshd."""
        _require_real_home_ok()
        if shutil.which("wt") is None:
            pytest.skip("Windows Terminal (wt) not on PATH")
        from magent import psmux as psmux_mod
        from magent.grid import compute_grid
        from magent.platform import get_platform
        from magent.titles import make_title

        if psmux_mod.find_psmux() is None:
            pytest.skip("psmux not installed (CI installs it via choco for this leg)")

        plat = get_platform()
        plat.set_dpi_aware()
        monitors = plat.list_monitors()
        assert monitors, "no real monitors detected"
        # attach tiles into a hardcoded 2x1 grid (cli/attach.py::_tile_titles).
        slots = compute_grid(monitors, 2, 1)
        if len(slots) < 2:
            pytest.skip("real display cannot host a 2x1 grid (DPI floor collapsed it)")

        unique = uuid.uuid4().hex[:8]
        # No dots/colons/spaces: psmux session id == project title, so the
        # remote session, the window title and the cleanup key all agree.
        name_a, name_b = f"mdssha{unique}", f"mdsshb{unique}"
        titles = [make_title(name_a), make_title(name_b)]
        proj_a = tmp_path / name_a
        proj_b = tmp_path / name_b
        proj_a.mkdir()
        proj_b.mkdir()
        upload_port = _free_port()

        config_body = json.dumps(
            {
                "version": 3,
                "projects": [
                    {"path": str(proj_a), "title": name_a},
                    {"path": str(proj_b), "title": name_b},
                ],
                "settings": {
                    "defaultTool": "probe",
                    "psmux": True,
                    "uploadServer": False,
                    "uploadPort": upload_port,
                    "tools": {"probe": f"echo mdssh-live-{unique}"},
                },
            }
        )
        # The remote `magent up --json` runs with the ssh user's real HOME
        # and no --config: seed both places find_config() looks on the far
        # side -- the session cwd (sshd starts commands in %USERPROFILE%) and
        # the canonical APPDATA path. Same user on both ends of the loopback.
        home = Path.home()
        from magent.env import config_base

        seeded = _seed_files(
            [
                (home / "magent.config.json", config_body),
                (config_base() / "magent" / "config.json", config_body),
            ]
        )

        hotkey_pre: int | None = None
        with suppress(ImportError):
            from magent.hotkey import listener_pid

            hotkey_pre = listener_pid()

        # Windows Terminal cold start races attach's two rapid `wt -w new`
        # spawns into ONE merged window (observed live in CI run 3: tiling
        # found neither title within its budget and only the second magent: title
        # ever existed as a top-level window). A real user attaches with a
        # warm terminal broker; give the test the same reality: open one
        # throwaway window first and hold it open across the attach run. Its
        # non-magent title is invisible to attach's magent-name tiling, and cleanup
        # closes it with everything else.
        warm_title = f"mdwarm-{unique}"
        subprocess.Popen(
            [
                "wt",
                "-w",
                "new",
                "--title",
                warm_title,
                "--suppressApplicationTitle",
                "--",
                "cmd",
                "/c",
                "ping",
                "-n",
                "900",
                "127.0.0.1",
            ]
        )
        warm_ok = _wait_until(lambda: warm_title in plat.snapshot_windows(), timeout=45)

        try:
            assert warm_ok, "Windows Terminal never opened the pre-warm window"
            rc, out, err = _run_to_files(
                [
                    sys.executable,
                    "-m",
                    "magent",
                    "attach",
                    ssh_wire["host"],
                    "-y",
                ],
                tmp_path,
                "attach",
                timeout=300,
            )
            assert rc == 0, f"attach exited {rc}\nstdout:\n{out}\nstderr:\n{err}"

            # 1. The REMOTE bring-up (a plain `ssh mdssh "magent up"`)
            #    created real psmux sessions -- and they survived that ssh
            #    session closing (Windows OpenSSH kills the command's job
            #    object on disconnect; surviving it is the product contract
            #    the whole attach flow rests on).
            for sid in (name_a, name_b):
                assert psmux_mod.has_session(sid), (
                    f"psmux session {sid!r} is not alive after the ssh "
                    f"bring-up returned -- sessions did not survive the ssh "
                    f"session closing\nattach stdout:\n{out}"
                )

            # 2. Real wt windows exist with the exact magent:<sid> titles, each
            #    hosting `ssh -t mdssh "magent sessions <sid>"`.
            def _both_windows() -> dict[str, object] | None:
                snap = _snapshot_titles(plat, titles)
                return snap if len(snap) == 2 else None

            handles = _wait_until(_both_windows, timeout=30)
            assert handles, (
                f"expected windows {titles}; magent: windows visible: "
                f"{[t for t in plat.snapshot_windows() if str(t).startswith('magent:')]}"
                f"\nattach stdout:\n{out}"
            )

            # 3. Tiling placed them: each window's center sits in its computed
            #    2x1 cell (generous by design -- wt chrome/DPI rounding).
            class _R(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            for title, slot in zip(titles, slots[:2], strict=True):
                r = _R()
                assert ctypes.windll.user32.GetWindowRect(
                    handles[title], ctypes.byref(r)
                )
                cx, cy = (r.left + r.right) / 2, (r.top + r.bottom) / 2
                assert slot.x <= cx <= slot.x + slot.w, (
                    f"{title}: center_x {cx} outside its cell "
                    f"[{slot.x}, {slot.x + slot.w}]\nattach stdout:\n{out}"
                )
                assert slot.y <= cy <= slot.y + slot.h, (
                    f"{title}: center_y {cy} outside its cell "
                    f"[{slot.y}, {slot.y + slot.h}]\nattach stdout:\n{out}"
                )

            # 4. `magent serve --ensure`, sent over its own ssh session,
            #    left a detached upload server that outlived it: /health on
            #    the configured (uuid-free but uuid-chosen) port answers.
            assert _wait_until(lambda: _health_ok(upload_port), timeout=20), (
                f"upload server ensured over ssh is not answering /health on "
                f"port {upload_port}\nattach stdout:\n{out}"
            )
        finally:
            leftovers = _close_windows_and_verify_gone(plat, [*titles, warm_title])
            killed = psmux_mod.kill_servers([name_a, name_b])
            _kill_upload_server(upload_port)
            with suppress(ImportError):
                from magent.hotkey import listener_pid

                hotkey_now = listener_pid()
                if hotkey_now and hotkey_now != hotkey_pre:
                    _taskkill(hotkey_now)
                    with suppress(OSError):
                        (Path.home() / ".magent" / "hotkey.pid").unlink()
            _restore_files(seeded)

        # Cleanup is part of the contract: nothing this test created survives.
        assert not leftovers, f"cleanup left real windows behind: {leftovers}"
        assert killed == [name_a, name_b]
        assert _wait_until(
            lambda: (
                not (psmux_mod.has_session(name_a) or psmux_mod.has_session(name_b))
            ),
            timeout=10,
        ), "psmux sessions survived kill_servers"


# ---------------------------------------------------------------------------
# 3. Linux: real `--go` remote launch -- the nested ssh quoting, live
# ---------------------------------------------------------------------------


def _xdotool_ids(title: str) -> list[str]:
    import re

    r = subprocess.run(
        ["xdotool", "search", "--name", f"^{re.escape(title)}$"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return r.stdout.split()


def _linux_kill_windows(titles: list[str]) -> list[str]:
    """Kill exactly the uuid-titled xterms (TERM then KILL their pids; the
    dying pty cascades SIGHUP through `ssh -t` to the remote shell)."""
    for round_sig in ("-TERM", "-KILL"):
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
                    subprocess.run(
                        ["kill", round_sig, pid], capture_output=True, check=False
                    )
        if _wait_until(lambda: not any(_xdotool_ids(t) for t in titles), timeout=10):
            break
    return [f"window {t}" for t in titles if _xdotool_ids(t)]


@pytest.mark.skipif(
    sys.platform != "linux", reason="real remote-launch render leg is linux-only"
)
class TestRemoteLaunchOverRealSshLinux:
    def test_go_remote_launch_marker_over_live_sshd_linux(self, tmp_path, ssh_wire):
        """A real `--go` (never --dry-run) launches an xterm running
        `ssh -t mdssh "bash -lc 'cd <dir> && touch <marker> && sleep 300'"`.
        The marker appearing on the far side proves launch.py's nested
        remote quoting executes over a live connection, end to end."""
        if not os.environ.get("DISPLAY"):
            pytest.skip("DISPLAY not set: no X server to host the real xterm")
        for tool in ("xterm", "xdotool"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} not installed: required for this leg")

        from magent.platform import get_platform
        from magent.titles import make_title

        unique = uuid.uuid4().hex[:8]
        name = f"mdsshl{unique}"
        title = make_title(name)
        remote_proj = tmp_path / "remote-proj"
        remote_proj.mkdir()
        marker = tmp_path / f"marker-{unique}"
        assert " " not in str(marker), "marker path must survive nested quoting"

        home = tmp_path / "home"
        home.mkdir()
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "version": 3,
                    "layout": {"columns": 1, "rows": 1},
                    "projects": [
                        {
                            "path": str(remote_proj),
                            "host": ssh_wire["host"],
                            "title": name,
                        }
                    ],
                    "settings": {
                        "defaultTool": "probe",
                        "settleSeconds": 1,
                        "launchDelayMs": 400,
                        "psmux": False,
                        "uploadServer": False,
                        # Runs on the FAR side of the wire; benign and
                        # long-lived so the window stays for the assertions.
                        "tools": {"probe": f"touch {marker} && sleep 300"},
                        "ssh": {"shell": "bash -lc"},
                    },
                }
            )
        )

        # Child env: HOME redirected (its ~/.magent never touches the real
        # user's) but the ssh CLIENT resolves ~/.ssh from passwd, not $HOME,
        # so the real ~/.ssh/config mdssh alias (CI-provisioned) still applies.
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")
        }
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")

        try:
            rc, out, err = _run_to_files(
                [sys.executable, "-m", "magent", "--go", "--config", str(cfg)],
                tmp_path,
                "go-remote",
                timeout=120,
                env=env,
                cwd=str(tmp_path),
            )
            assert rc == 0, f"--go failed\nstdout:\n{out}\nstderr:\n{err}"

            # 1. THE quoting proof: the remote command ran and touched the
            #    marker through xterm -> ssh -t -> bash -lc -> cd && touch.
            assert _wait_until(marker.exists, timeout=60), (
                f"marker {marker} never appeared: the nested ssh remote "
                f"command did not execute\nstdout:\n{out}\nstderr:\n{err}"
            )

            # 2. The real window exists with the exact magent: title...
            assert _wait_until(lambda: bool(_xdotool_ids(title)), timeout=20), (
                f"expected a real xterm titled {title!r}\nstdout:\n{out}"
            )
            assert get_platform().find_window(title) is not None

            # 3. ...and tiling resolved it (never fell to "not found").
            assert "not found" not in out, (
                f"tiling gave up on the remote window:\n{out}"
            )
        finally:
            leftovers = _linux_kill_windows([title])
            # The xterm's death drops the ssh -t session; SIGHUP kills the
            # remote `sleep`. Belt and braces: TERM any straggler by marker.
            subprocess.run(
                ["pkill", "-TERM", "-f", str(marker)],
                capture_output=True,
                check=False,
            )
            with suppress(OSError):
                marker.unlink()

        assert not leftovers, f"cleanup left real windows behind: {leftovers}"
        assert _wait_until(
            lambda: (
                subprocess.run(
                    ["pgrep", "-f", str(marker)], capture_output=True, check=False
                ).returncode
                != 0
            ),
            timeout=10,
        ), "remote-launched process (marker cmdline) survived window teardown"


# ---------------------------------------------------------------------------
# 4. macOS: window legs are a LOUD skip (TCC), never a quiet green
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS loud-skip leg is darwin-only"
)
class TestRemoteWindowLegMacos:
    def test_remote_window_leg_macos_loud_skip(self, ssh_wire, capsys):
        """No macOS window-over-ssh coverage exists -- say so loudly.

        Terminal.app automation is TCC-gated and blocked on hosted runners
        (established by the tests/platform macOS render leg, PR #47), so a
        real `--go`-over-ssh window here is unattainable in CI. The
        windowless wire coverage (TestSshControlChannel) still runs on
        macOS. This test never fakes the window leg: it emits a GitHub
        ::warning and skips, in both the TCC-blocked and TCC-permitted
        cases (the macOS ssh+Terminal.app launch path is unverified on real
        hardware -- see the note in DESIGN.md)."""
        if not shutil.which("osascript"):
            pytest.skip("osascript not available")
        probe = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to count processes'],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        automation_ok = probe.returncode == 0 and probe.stdout.strip().isdigit()
        if not automation_ok:
            _emit_ci_warning(
                capsys,
                "macOS SSH window leg skipped (TCC)",
                "UI automation is TCC-blocked on this runner; the remote-launch "
                "window leg cannot run on macOS CI (the windowless real-ssh wire "
                "coverage in TestSshControlChannel does). Not a green pass.",
            )
            pytest.skip("macOS UI automation TCC-blocked: window-over-ssh leg unrun")
        _emit_ci_warning(
            capsys,
            "macOS SSH window leg not implemented",
            "UI automation is permitted here, but the macOS ssh+Terminal.app "
            "launch path is unverified on real hardware and has no CI story; "
            "skipping rather than asserting theatre. See DESIGN.md.",
        )
        pytest.skip("macOS window-over-ssh leg intentionally unimplemented (loud)")
