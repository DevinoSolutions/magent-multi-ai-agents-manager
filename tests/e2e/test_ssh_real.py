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
  the ssh session closing, real ``wt`` windows open with exact ``magent:``
  titles running the reconnect supervisor over a real ssh client, those panes
  read as ALIVE to the product's own live process scan (the only proof that
  wt + the Windows console-script launcher + CIM keep the ``-L <sid> attach``
  marker visible on a real spawn -- all three halves are OS behavior no unit
  test can reach), tiling places them into their computed cells, and
  ``serve --ensure`` (sent over ssh) leaves a live upload server answering
  ``/health`` after its ssh session is gone (the spawn_detached
  job-object-breakaway contract).
* ``test_go_remote_launch_marker_over_live_sshd_linux`` -- a real ``--go``
  (no dry-run) drives launch.py's ssh branch: ``xterm -e "cd <cwd> && ssh -t
  mdssh \"bash -lc 'cd <dir> && touch <marker> && sleep 300'\""``. The marker
  file appearing proves the full nested-quoting chain executed on the far
  side of a real connection.
* ``TestSessionsOutliveTheirSshConnection`` (win32) -- the survival contract
  the whole remote workflow rests on, tested the only way that can see it: KILL
  THE CONNECTION while a session is still up, and assert the session (and the
  work in it) is still there afterwards. Both connections a session can have
  are covered: the one that CREATED it (a live ``ssh <host> "magent up"``) and
  one ATTACHED to it. The flagship above only ever closes its ssh session
  gracefully. Read the class docstring for what these do and do not prove.
* ``TestReconnectSupervisorOverRealSsh`` (all OSes) -- the reconnect
  supervisor each attach pane runs (``magent-attach-client``) driving a REAL
  ssh client. A dial at an unroutable address produces a real client-side
  exit 255 and the supervisor really redials after the real 2s backoff (the
  reported bug's exact shape); through the live sshd, a remote command that
  ends while the session is NOT on the host is retried and only then given up
  on (identically on every OS -- that sameness is the fix), and
  ``--no-reconnect`` still stops on the first exit. The retry leg also pins a
  platform finding no other tier can see: Windows OpenSSH does not propagate a
  remote exit status over a pty, which is why exit code alone may never decide
  a pane's fate (read its docstring before touching the decision table).
* ``TestTypedTextSurvivesARealDrop`` (all OSes) -- the user-facing half of the
  reconnect story, and the only tier that can show both halves at once: a real
  session over a real pty, text typed into it, the ssh CLIENT killed
  out-of-band (a wi-fi failure, not a remote command choosing to exit), and
  then the redial. Asserts the typed text is still on screen DURING the outage
  on a row the status line never touched, and that the reattach restores the
  HOST's copy of it. The local rendering guarantee is pinned cell by cell,
  without a network, by ``tests/e2e/test_pty_attach_status.py``.
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
            #    hosting the reconnect supervisor, which in turn holds the
            #    `ssh -t mdssh "psmux -L <sid> attach || ..."` client.
            def _both_windows() -> dict[str, object] | None:
                snap = _snapshot_titles(plat, titles)
                return snap if len(snap) == 2 else None

            handles = _wait_until(_both_windows, timeout=30)
            assert handles, (
                f"expected windows {titles}; magent: windows visible: "
                f"{[t for t in plat.snapshot_windows() if str(t).startswith('magent:')]}"
                f"\nattach stdout:\n{out}"
            )

            # 2b. The panes read as ALIVE to the product's own corpse scan.
            #     This is the one place the whole coupling is proven against a
            #     real spawn: wt actually started `magent-attach-client`, the
            #     Windows console-script launcher kept the argv (and therefore
            #     the `-L <sid> attach` marker) on a process whose name is in
            #     _CLIENT_PROCESS_NAMES, and a real CIM scan finds it. If any
            #     link broke, every pane would be swept as a corpse the moment
            #     it started backing off -- and no unit test can catch that,
            #     because all three halves are OS behavior.
            from magent.cli.attach import _dead_sids

            assert _wait_until(lambda: not _dead_sids({name_a, name_b}), timeout=30), (
                "attach panes read as corpses to the live process scan: "
                f"{_dead_sids({name_a, name_b})}\nattach stdout:\n{out}"
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
# 2b. The session-survival contract: a dead CONNECTION must not be a dead
#     SESSION. Windows-only, because the job object that used to couple the
#     two is a Windows OpenSSH construct and POSIX sshd has no equivalent.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "job-object session teardown is Windows OpenSSH behavior; POSIX sshd "
        "has no equivalent, so there is nothing here for a POSIX leg to prove"
    ),
)
class TestSessionsOutliveTheirSshConnection:
    """A client disconnect must never kill work on the server.

    THE INCIDENT this exists for: a laptop on flaky wi-fi attached to a Windows
    host. ``magent attach`` sends ``magent up`` over SSH, and Windows OpenSSH
    runs every session command inside a job object marked kill-on-close. Job
    membership is inherited by every descendant, so each psmux SERVER that
    bring-up created -- and the agent inside it -- was born owned by the WI-FI.
    One flap and sshd tore the job down: 45 sessions at 10:50, 16 at 11:03,
    with no magent process running in between, and the survivors were exactly
    the sessions that had been created locally on the host.

    No other tier can see this. The flagship attach test above closes its ssh
    session GRACEFULLY, after the remote command returned; these kill the
    connection out from under a live session.

    HONEST RESULT, measured (PR #160, a control run with the breakaway spawn
    reverted to a plain ``Popen``): on ``windows-latest`` with psmux 3.3.6
    these PASS either way. So the job object is a real hazard the product must
    not rely on luck to avoid -- ``procs.spawn_unjobbed`` closes it, and the
    repo already documented the mechanism in ``launch.spawn_detached`` -- but
    it is NOT a reproduction of the reporter's session deaths, and nothing here
    should be read as having proven that cause. These are contract tests: they
    guard the property the whole remote workflow assumes, on both the creating
    connection and the attached one, and they go red if magent or psmux ever
    starts coupling a session's life to a socket.
    """

    def test_a_session_created_over_ssh_survives_the_connection_dying(
        self, tmp_path, ssh_wire
    ):
        _require_real_home_ok()
        from magent import psmux as psmux_mod
        from magent.env import config_base

        if psmux_mod.find_psmux() is None:
            pytest.skip("psmux not installed (CI installs it via choco for this leg)")

        unique = uuid.uuid4().hex[:8]
        name = f"mdsurv{unique}"
        proj = tmp_path / name
        proj.mkdir()
        # A long-lived, harmless pane command: the session must still be there
        # a moment later for its own reasons, not because a fast agent exited.
        config_body = json.dumps(
            {
                "version": 3,
                "projects": [{"path": str(proj), "title": name}],
                "settings": {
                    "defaultTool": "probe",
                    "psmux": True,
                    "uploadServer": False,
                    "tools": {"probe": "ping -n 900 127.0.0.1"},
                },
            }
        )
        home = Path.home()
        seeded = _seed_files(
            [
                (home / "magent.config.json", config_body),
                (config_base() / "magent" / "config.json", config_body),
            ]
        )

        target = _ssh_target(ssh_wire["host"])
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", target, "magent up"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        try:
            # The session must exist while the connection that made it is
            # STILL OPEN -- killing it after `magent up` returned would only
            # re-test the graceful close the flagship already covers.
            assert _wait_until(
                lambda: psmux_mod.has_session(name), timeout=180, interval=0.5
            ), f"remote bring-up never created {name!r}"
            assert proc.poll() is None, (
                "the ssh command finished before the session could be caught "
                "mid-connection; this leg needs a slower bring-up to be honest"
            )

            # The wi-fi flap, faithfully: the ssh CLIENT dies without warning,
            # the transport drops, and sshd tears the session (and its job)
            # down. Nothing asks psmux to stop.
            _kill_tree(proc)

            # sshd's teardown is not instantaneous, so a session that is going
            # to die needs to be given the chance to. A pass here means it
            # never had a job to be killed with.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert psmux_mod.has_session(name), (
                    f"psmux session {name!r} died with the SSH connection "
                    f"that created it -- the session-creation spawn is back "
                    f"inside sshd's kill-on-close job object, or psmux stopped "
                    f"detaching its server (see procs.spawn_unjobbed)"
                )
                time.sleep(1)
        finally:
            _kill_tree(proc)
            psmux_mod.kill_servers([name])
            _restore_files(seeded)

        assert _wait_until(lambda: not psmux_mod.has_session(name), timeout=15), (
            "psmux session survived kill_servers"
        )

    def test_a_session_survives_its_attached_client_dying(self, tmp_path, ssh_wire):
        """The other half, and the one that matches the incident's shape.

        The test above kills the connection that CREATED a session. This one
        kills a connection that is ATTACHED to a live session -- which is what
        a wi-fi flap actually does to a user with forty open attach panes. If a
        psmux server followed its client into the grave, every attached session
        would die at once and every unattached one would survive, which is
        precisely the pattern the reporter saw (45 sessions -> 16, with no
        magent process running in between).

        The session is created LOCALLY here on purpose, so the two variables
        stay separated: whatever this asserts is about the ATTACH connection
        alone, with no job object from a creating connection in the picture.

        A tmux-family server orphans its clients rather than dying with them,
        so this should hold -- it is a contract test guarding a property the
        whole remote workflow assumes, not a reproduction of a known break.
        """
        from magent import psmux as psmux_mod
        from magent.platform import PsmuxWindowOpts
        from magent.platform.windows import WindowsPlatform

        binary = psmux_mod.find_psmux()
        if binary is None:
            pytest.skip("psmux not installed (CI installs it via choco for this leg)")

        name = f"mdatt{uuid.uuid4().hex[:8]}"
        proj = tmp_path / name
        proj.mkdir()
        target = _ssh_target(ssh_wire["host"])
        proc: subprocess.Popen | None = None
        try:
            WindowsPlatform().launch_psmux_session(
                [
                    PsmuxWindowOpts(
                        window_name=name,
                        cwd=str(proj),
                        command="ping -n 900 127.0.0.1",
                    )
                ]
            )
            assert _wait_until(
                lambda: psmux_mod.has_session(name), timeout=60, interval=0.5
            ), f"local bring-up never created {name!r}"

            # A real interactive attach over a real connection -- `-t`, exactly
            # as an attach pane runs it.
            proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-t", target, f"psmux -L {name} attach"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

            def _client_attached() -> bool:
                out = subprocess.run(
                    [binary, "-L", name, "list-clients"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                return out.returncode == 0 and bool(out.stdout.strip())

            if not _wait_until(_client_attached, timeout=60, interval=1.0):
                # Never assert a pass that was not earned: with no client, the
                # kill below proves nothing about session survival.
                pytest.skip(
                    "no psmux client ever registered for the ssh attach; this "
                    "leg cannot prove anything without one"
                )

            # The flap: the attached ssh client dies without warning.
            _kill_tree(proc)

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert psmux_mod.has_session(name), (
                    f"psmux session {name!r} died when the client ATTACHED to "
                    f"it was killed -- a server must orphan its clients, not "
                    f"follow them. This is the shape of the reported incident."
                )
                time.sleep(1)
        finally:
            if proc is not None:
                _kill_tree(proc)
            psmux_mod.kill_servers([name])

        assert _wait_until(lambda: not psmux_mod.has_session(name), timeout=15), (
            "psmux session survived kill_servers"
        )


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
# 4. All OSes: the reconnect supervisor, over the real wire
# ---------------------------------------------------------------------------


def _attach_client_exe() -> str:
    """The installed reconnect supervisor, or a clean skip.

    Resolved exactly the way ``cli/attach.py`` resolves it -- by name, off PATH
    -- so this tier fails if a pane could not have started it either."""
    from magent.attach_client import CLIENT_EXE_NAME

    exe = shutil.which(CLIENT_EXE_NAME)
    if exe is None:
        pytest.skip(f"{CLIENT_EXE_NAME} console script not installed on PATH")
    return exe


def _remote_python(target: str) -> str:
    """A python the REMOTE shell can run, or a clean skip.

    These tests need a remote command with a command-line-chosen exit code, and
    the sshd's shell is cmd.exe on Windows and ``$SHELL -c`` elsewhere -- the
    one spelling both parse identically is a quoted ``python -c``."""
    from magent.cli.attach import _ssh_capture

    for candidate in ("python", "python3"):
        rc, _out, _err = _ssh_capture(target, f"{candidate} -c \"print('ok')\"", 60)
        if rc == 0:
            return candidate
    pytest.skip("no python on the remote sshd PATH to script exit codes with")
    raise AssertionError("unreachable")  # pragma: no cover


def _exit_with(python: str, code: int, marker: Path | None = None) -> str:
    """A remote command that (optionally) appends a line to ``marker`` and then
    exits ``code``. Same machine on both ends of the loopback wire, so the
    marker path the local test reads is the one the remote process writes."""
    body = ""
    if marker is not None:
        body = f"open(r'{marker.as_posix()}','a').write('x\\n'); "
    return f'{python} -c "{body}raise SystemExit({code})"'


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a supervisor and everything under it.

    The Windows console-script launcher runs the real python as a CHILD, and
    that child owns the ssh grandchild, so terminating only the process we
    spawned would leave a live ssh behind for the next test to trip over."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=30,
            check=False,
        )
    else:
        proc.kill()
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=30)


# TEST-NET-1 (RFC 5737): guaranteed never routed, so a dial at it always fails
# in connect() and the local ssh CLIENT emits the real exit 255. That is the
# faithful reproduction of the reported bug -- a laptop that slept, a wi-fi
# change, a host that rebooted all surface as a client-side 255, generated
# here rather than reported by a server. Deliberately NOT simulated with a
# remote command that exits 255: see the Windows note on the sshd legs below.
_UNROUTABLE = "192.0.2.1"


def _connect_timeout_s() -> int:
    """The product's own ConnectTimeout, so this file's waiting budget tracks
    it instead of hard-coding a number that would silently go too tight."""
    from magent.attach_client import SSH_CONNECTION_OPTS

    for opt in SSH_CONNECTION_OPTS:
        if opt.startswith("ConnectTimeout="):
            return int(opt.split("=", 1)[1])
    raise AssertionError(f"no ConnectTimeout in {SSH_CONNECTION_OPTS}")


class TestReconnectSupervisorOverRealSsh:
    """`magent-attach-client` driving a REAL ssh client.

    The unit tier scripts ssh's exit codes and fakes the session probe; nothing
    there proves the supervisor can drive a real ssh client at all, or that
    OpenSSH behaves the way the decision table assumes. The two sshd legs are
    genuine key-authenticated sessions through the CI loopback server -- and
    they now exercise the REAL out-of-band probe too, which on a runner with no
    such session honestly answers "gone". The redial leg deliberately fails to
    reach a host instead, because that is where a real 255 comes from.
    """

    def test_a_dropping_connection_is_redialled_for_real(self, tmp_path, ssh_wire):
        """The headline behavior: the pane heals itself, with no second
        `magent attach` and no human.

        A real ssh client, a real unreachable host, a real 255, a real backoff,
        a real second dial -- and the supervisor still standing afterwards,
        which is what "retry forever" means.
        """
        exe = _attach_client_exe()
        out_path = tmp_path / "supervisor.out"

        with out_path.open("w", encoding="utf-8") as fo:
            proc = subprocess.Popen(
                [
                    exe,
                    "--target",
                    f"probe@{_UNROUTABLE}",
                    "--session",
                    "reconnect-probe",
                    "--remote",
                    "psmux -L reconnect-probe attach",
                ],
                stdout=fo,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )

            def _text() -> str:
                return out_path.read_text(encoding="utf-8", errors="replace")

            try:
                # Dial 1 fails at ConnectTimeout, the ladder prints, the backoff
                # elapses, dial 2 starts. Budget covers two connect timeouts on
                # a cold runner; the status lines are explicitly flushed by the
                # product, which is what makes reading them mid-run possible.
                budget = 3 * _connect_timeout_s() + 30
                assert _wait_until(
                    lambda: "reconnecting to" in _text(), timeout=budget, interval=0.5
                ), f"supervisor never redialled\noutput:\n{_text()}"
                # ...and it did NOT give up: retry forever means still alive.
                assert proc.poll() is None, (
                    f"supervisor exited instead of reconnecting\noutput:\n{_text()}"
                )
            finally:
                _kill_tree(proc)

        text = _text()
        assert "connection to" in text and "lost" in text, text
        assert "ssh exit 255" in text, text
        # Redirected to a file, so the pane takes the plain-line fallback --
        # no carriage-return animation, one readable line per attempt.
        assert "retry in 2s" in text, text
        assert "attempt 1" in text, text
        assert "\x1b[" not in text, text

    def test_a_vanished_session_is_retried_then_given_up_on(self, tmp_path, ssh_wire):
        """The reported bug, against a real sshd: a remote command that ends
        while the session is NOT on the host must not close the pane.

        Before the session probe existed this connected once and stopped -- on
        a Windows host announcing a "detach" the user never asked for, because
        Windows OpenSSH reports 0 for a remote command that failed over a pty
        (see the finding pinned below). Now the supervisor asks the host, over
        a separate non-pty connection, whether the session is still there.
        ``retry-probe`` is not a session on any runner, so the honest answer is
        "gone" and the pane keeps trying -- exactly what a user wants while a
        host is rebooting or a 45-session ``magent up`` is still working.

        The other half of the contract is in the same assertion: it gives up
        after ``SESSION_MISSING_MAX`` tries rather than dialling a healthy sshd
        forever over a session that is never coming back.

        FINDING, pinned here because it is invisible to every other tier:
        **Windows OpenSSH does not propagate a remote command's exit status
        over a pty session.** ``ssh -t win-host "exit 7"`` reports 0, where
        POSIX sshd reports 7. That asymmetry is why exit code alone can no
        longer decide a pane's fate, and why the probe deliberately drops
        ``-t``. The assertions below are IDENTICAL on both platforms now --
        that sameness is the point, and it is what regressed for Windows users
        when the decision rested on rc.
        """
        from magent.attach_client import SESSION_MISSING_MAX

        exe = _attach_client_exe()
        target = _ssh_target(ssh_wire["host"])
        python = _remote_python(target)
        marker = tmp_path / f"retry-{uuid.uuid4().hex[:8]}.log"

        rc, out, err = _run_to_files(
            [
                exe,
                "--target",
                target,
                "--session",
                "retry-probe",
                "--remote",
                _exit_with(python, 7, marker),
            ],
            tmp_path,
            "session-missing",
            timeout=600,
        )
        dials = marker.read_text(encoding="utf-8").splitlines()
        assert len(dials) == SESSION_MISSING_MAX, (
            f"expected exactly {SESSION_MISSING_MAX} dials before giving up, "
            f"got {len(dials)}\nstdout:\n{out}\nstderr:\n{err}"
        )
        assert "retry-probe is not on" in out, out
        assert "is not a session there" in out, out
        # Never 0: a pane that gave up must not read as a clean detach to
        # whatever inspects its exit code.
        assert rc != 0, f"stdout:\n{out}"

    def test_no_reconnect_still_stops_on_the_first_exit(self, tmp_path, ssh_wire):
        """``--no-reconnect`` promises the historical bare-ssh pane, and this
        is what proves the new probe did not quietly break that promise: one
        connection, one exit code, no second dial of any kind."""
        exe = _attach_client_exe()
        target = _ssh_target(ssh_wire["host"])
        python = _remote_python(target)
        marker = tmp_path / f"once-{uuid.uuid4().hex[:8]}.log"

        rc, out, err = _run_to_files(
            [
                exe,
                "--target",
                target,
                "--session",
                "once-probe",
                "--remote",
                _exit_with(python, 7, marker),
                "--no-reconnect",
            ],
            tmp_path,
            "no-reconnect",
            timeout=120,
        )
        assert marker.read_text(encoding="utf-8").splitlines() == ["x"], (
            f"--no-reconnect dialled more than once; stdout:\n{out}"
        )
        if sys.platform == "win32":
            # The pty caveat again: a Windows host reports 0 for the failing
            # remote command, so the rc-only table reads a clean detach.
            assert rc == 0, f"stdout:\n{out}\nstderr:\n{err}"
            assert "detached from once-probe" in out
        else:
            assert rc == 7, f"stdout:\n{out}\nstderr:\n{err}"
            assert "could not be attached" in out


# ---------------------------------------------------------------------------
# 4b. All OSes: typed-but-unsent text survives a real drop, over a real pty
# ---------------------------------------------------------------------------


# A stand-in for the remote SESSION -- the thing psmux would be holding open.
# It keeps its "screen" in a file on the host, redraws that file on every
# attach (which is what psmux does on reattach), and appends whatever is typed
# at it. That is the whole of what the user's question depends on: their unsent
# sentence lives in the REMOTE process, so a dropped client cannot lose it and
# a reattach must show it again. Loopback sshd, so "a file on the host" is a
# file this test can also read.
_REMOTE_SESSION_PY = """\
import sys, time
buf = sys.argv[1]
try:
    prev = open(buf, encoding="utf-8").read().replace("\\n", " ").strip()
except OSError:
    prev = ""
# The alternate screen, entered exactly as psmux/an agent TUI enters it: this
# is what leaves the local terminal frozen mid-frame when the link dies.
sys.stdout.write("\\x1b[?1049h\\x1b[2J\\x1b[1;1H")
sys.stdout.write("REMOTE-READY\\r\\n")
sys.stdout.write("BUFFER>" + prev + "<\\r\\n")
sys.stdout.flush()
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    line = sys.stdin.readline()
    if not line:
        break
    text = line.strip()
    if not text:
        continue
    with open(buf, "a", encoding="utf-8") as fh:
        fh.write(text + "\\n")
    sys.stdout.write("ECHO>" + text + "<\\r\\n")
    sys.stdout.flush()
"""


def _pty_backend_or_skip() -> None:
    if sys.platform == "win32":
        pytest.importorskip("winpty", reason="pywinpty needed to drive a real pty")
    else:
        pytest.importorskip("pexpect", reason="pexpect needed to drive a real pty")


def _kill_ssh_carrying(token: str) -> None:
    """Kill the ssh CLIENT whose command line carries ``token``, and only it.

    Out-of-band on purpose: the drop has to look to the supervisor exactly like
    a wi-fi failure -- something outside the process killing the connection --
    rather than a remote command choosing to exit. Narrowed to processes
    actually named ``ssh`` so the supervisor itself, whose argv carries the same
    token in ``--remote``, is never the one that dies.
    """
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{token}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        return
    found = subprocess.run(
        ["pgrep", "-f", token], capture_output=True, text=True, timeout=30, check=False
    )
    for pid in found.stdout.split():
        comm = subprocess.run(
            ["ps", "-o", "comm=", "-p", pid],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if os.path.basename(comm.stdout.strip()) == "ssh":
            with suppress(OSError, ValueError):
                os.kill(int(pid), 9)


class TestTypedTextSurvivesARealDrop:
    """The user's actual question, answered over the real wire.

    "When we get the reconnecting warning, don't replace the text that is
    written in Claude Code, because we may have text typed from before that
    we'd want to still send." Two halves, and only a real connection can show
    both at once:

    * the LOCAL half -- the reconnect status line must not erase the frozen
      frame the user is looking at. Pinned in detail, grid cell by grid cell,
      by the real-pty tier (``test_pty_attach_status.py``), which can stage a
      frozen frame deterministically without a network.
    * the REMOTE half -- the sentence itself lives in the remote process, so
      killing the connection cannot lose it and the reattach must bring it
      back. THAT is what needs a real ssh, a real out-of-band kill, and a real
      redial, and it is what this test is for.
    """

    def test_typed_text_survives_a_real_reconnect(self, tmp_path, ssh_wire):
        _pty_backend_or_skip()
        from tests.e2e._pty import Pty
        from tests.e2e._screen import Screen

        exe = _attach_client_exe()
        target = _ssh_target(ssh_wire["host"])
        python = _remote_python(target)
        token = f"unsent{uuid.uuid4().hex[:8]}"
        script = tmp_path / f"{token}-session.py"
        script.write_text(_REMOTE_SESSION_PY, encoding="utf-8")
        buffer = tmp_path / f"{token}-buffer.txt"
        remote = f'{python} "{script.as_posix()}" "{buffer.as_posix()}"'

        rows, cols = 24, 80
        pty = Pty(
            [exe, "--target", target, "--session", token, "--remote", remote],
            # COLUMNS/LINES stripped for the same reason the pty tier strips
            # them: `shutil.get_terminal_size` prefers them over the real pty,
            # and the bottom-row assertions below depend on the real one.
            env={
                k: v
                for k, v in os.environ.items()
                if k.upper() not in ("PYTHONPATH", "COLUMNS", "LINES")
            },
            cwd=str(tmp_path),
            dimensions=(rows, cols),
        )
        try:
            # 1. A real session, over a real connection.
            pty.expect("REMOTE-READY", timeout=180)
            # 2. The user types, and it lands on the HOST.
            pty.send_line(token)
            pty.expect(f"ECHO>{token}<", timeout=120)
            assert _wait_until(
                lambda: buffer.exists() and token in buffer.read_text(encoding="utf-8"),
                timeout=60,
            ), "the typed text never reached the host"

            # 3. The wi-fi "drops": something outside the pane kills the client.
            _kill_ssh_carrying(token)

            # 4. The pane heals itself, and while it does, the status line is
            #    on screen -- this is the moment the old code erased the frame.
            pty.expect("reconnecting", timeout=180)
            mid_outage = Screen(rows=rows, cols=cols).feed(pty.raw)

            # 5. The reattach redraws the session, typed text and all.
            pty.expect(f"BUFFER>{token}<", timeout=300)
            healed = Screen(rows=rows, cols=cols).feed(pty.raw)
        finally:
            with suppress(Exception):
                pty.close()
            _kill_ssh_carrying(token)

        mid_report = f"\n--- mid-outage screen ---\n{mid_outage.text}"
        # The status line owns the bottom row and nothing else -- so whatever
        # the user was looking at above it is still there to read.
        assert "reconnecting" in mid_outage.line(rows - 1), (
            f"the status line is not on the bottom row{mid_report}"
        )
        assert sum("reconnecting" in line for line in mid_outage.lines) == 1, (
            f"the status line was drawn more than once{mid_report}"
        )
        # THE HEADLINE: the sentence the user typed and did not send is still
        # on the screen during the outage, on a row the status line never
        # touched.
        typed_row = mid_outage.row_of(f"ECHO>{token}<")
        assert typed_row not in (-1, rows - 1), (
            f"the typed text was erased by the reconnect warning{mid_report}"
        )
        # ...and after the redial it is the REMOTE's copy that comes back,
        # which is the reason it was never really at risk.
        assert f"BUFFER>{token}<" in healed.text, (
            f"the reattached session did not restore the typed text\n"
            f"--- healed screen ---\n{healed.text}"
        )


# ---------------------------------------------------------------------------
# 5. macOS: window legs are a LOUD skip (TCC), never a quiet green
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
