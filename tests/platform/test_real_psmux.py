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
  degradation the guard promises callers that fan this across sessions;
* ``psmux.idle_sessions`` reads a real pane as idle only while nothing magent
  typed into it is running -- see the section above its test -- and never
  while an orphan outside the pane's tree shares the pane's console.

Skips cleanly when psmux is not installed or the platform has no psmux
support (macOS/Linux, and CI runners without the binary).
"""

import os
import shutil
import subprocess
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

    # The sent command drops a file: rendering-independent proof of delivery
    # AND execution. (capture-pane is useless as the observable on headless
    # CI runners -- the pane shell runs but never renders into the virtual
    # screen, so its buffer stays empty forever.)
    marker = workdir / f"mdrl-{unique}.delivered"

    created = False
    try:
        # Bring the session up the way run_magent does: through the
        # platform primitive (real `psmux new-session -d -c <cwd>` + send-keys).
        get_platform().launch_psmux_session(
            [
                PsmuxWindowOpts(
                    window_name=name,
                    cwd=str(workdir),
                    command=f'echo delivered > "{marker}"',
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

        # The agent command really landed in the pane. Regression pin: the
        # send-keys senders used to be fire-and-forget, so a parent that exits
        # immediately (sshd tears down `magent up`'s process tree on channel
        # close -- the host side of attach) killed them before the keystrokes
        # arrived, leaving every session a bare shell with no agent running.
        if not _wait_until(marker.exists, timeout=20):
            binary = psmux.find_psmux() or "psmux"
            panes = subprocess.run(
                [
                    binary,
                    "-L",
                    name,
                    "list-panes",
                    "-F",
                    "cmd=#{pane_current_command} dead=#{pane_dead}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            cap = subprocess.run(
                [binary, "-L", name, "capture-pane", "-p"],
                capture_output=True,
                text=True,
                check=False,
            )
            pytest.fail(
                "send-keys command never executed (marker file absent)\n"
                f"  list-panes rc={panes.returncode} out={panes.stdout!r} err={panes.stderr!r}\n"
                f"  capture rc={cap.returncode} out={cap.stdout!r} err={cap.stderr!r}"
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


# --- idle_sessions against a REAL pane -----------------------------------------
#
# The unit tier proves idle_sessions' rule against fake snapshots. This proves
# the readings the rule rests on are what a real psmux pane produces: that
# #{pane_pid} is the pane's own shell, and that what magent types into a pane
# (`cmd /c <command>`, platform/windows.py::_send_argv) sits under that shell
# for exactly as long as the command runs. Each phase asks twice: the call as
# revive makes it, and once with the foreground forced to a shell -- the reading
# measured live while an agent runs its Bash tool -- so the process-tree half is
# proven on its own instead of hiding behind psmux's foreground filter.
#
# This tier has no HOME isolation, so the test touches nothing but its own
# private -L socket and tmp_path. The session is driven by raw psmux argv
# against that one socket, not launch_psmux_session (which decorates, verifies
# and can log under the real ~/.magent). The two commands it types into the
# pane end on their own bounds even if the teardown never runs; the psmux
# server and its pane shell do not, and die only by the finally block's
# kill_server -- the same posture as the lifecycle test above.


def test_real_pane_reads_idle_only_while_nothing_it_launched_runs(tmp_path):
    from magent.platform.windows import _ps_quote, _send_argv
    from magent.procs import process_tree, snapshot_processes

    binary = psmux.find_psmux()
    assert binary is not None  # module pytestmark guarantees it
    ping = shutil.which("ping")
    assert ping is not None, "PING.EXE not on PATH"
    holder_shell = "pwsh" if shutil.which("pwsh") else "powershell"

    unique = uuid.uuid4().hex[:12]
    name = f"mdrl-idl-{unique}"
    workdir = tmp_path / f"cwd-{unique}"
    workdir.mkdir()

    # The launched command: a shell-named child that says it started, then
    # holds until released (or 120s pass, whatever happens to the test).
    started = tmp_path / "started"
    release = tmp_path / "release"
    hold = tmp_path / "hold.ps1"
    hold.write_text(
        f"New-Item -ItemType File -Force -Path {_ps_quote(str(started))} | Out-Null\n"
        "$until = (Get-Date).AddSeconds(120)\n"
        f"while (-not (Test-Path -LiteralPath {_ps_quote(str(release))})"
        " -and (Get-Date) -lt $until) {\n"
        "    Start-Sleep -Milliseconds 200\n"
        "}\n",
        encoding="utf-8",
    )
    # The agent image: PING.EXE under the name claude.exe, alive ~19s -- room
    # for a tree poll and two idle reads on a loaded runner.
    stand_in = tmp_path / "claude.exe"
    shutil.copyfile(ping, stand_in)

    def run(*args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            [binary, "-L", name, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )

    def idle(forced: bool = False) -> bool:
        foreground = {name: "pwsh"} if forced else None
        return name in psmux.idle_sessions([name], psmux=binary, foreground=foreground)

    def pane_images() -> list[str]:
        pid = psmux.pane_pids([name], psmux=binary).get(name)
        tree = process_tree(pid, snapshot_processes() or []) if pid else None
        return [image.lower() for image, _pid, _ppid in tree or []]

    def diag() -> str:
        return (
            f"foreground={psmux.pane_current_commands([name], psmux=binary)!r}"
            f" pane tree={pane_images()!r}"
        )

    try:
        # env=child_env(): psmux's nesting guard refuses new-session from
        # inside a psmux pane while still exiting 0 (see launch_psmux_session).
        new = run(
            "new-session", "-d", "-s", name, "-c", str(workdir), env=psmux.child_env()
        )
        assert new.returncode == 0, f"new-session failed: {new.stderr!r}"
        assert _wait_until(lambda: psmux.has_session(name), timeout=10), (
            f"psmux session {name!r} never came up"
        )

        # 1. A pane at rest is idle -- the baseline every later phase departs
        #    from, and the reading revive acts on.
        assert _wait_until(idle, timeout=30), (
            f"a resting pane never read idle: {diag()}"
        )
        assert idle(forced=True), f"a resting pane's tree read busy: {diag()}"

        # 2. The launcher: magent's own send argv around a shell-named command.
        send = _send_argv(
            binary,
            PsmuxWindowOpts(
                window_name=name,
                cwd=str(workdir),
                command=(
                    f'{holder_shell} -NoProfile -ExecutionPolicy Bypass -File "{hold}"'
                ),
            ),
        )
        subprocess.run(send, capture_output=True, timeout=30, check=False)
        if not _wait_until(started.exists, timeout=15) and idle():
            # PSReadLine can swallow keys typed while it initialises (measured
            # on the bring-up path); one re-send, and only while the pane is
            # provably still at rest -- the rule revive itself follows.
            subprocess.run(send, capture_output=True, timeout=30, check=False)
        assert _wait_until(started.exists, timeout=30), (
            f"the launched command never ran: {diag()}"
        )
        for _ in range(4):
            assert not idle(), f"a pane running cmd /c <command> read idle: {diag()}"
            assert not idle(forced=True), (
                f"a live launcher under the pane's shell read idle: {diag()}"
            )
            time.sleep(0.5)

        release.touch()
        assert _wait_until(idle, timeout=30), (
            f"the pane never read idle after its command exited: {diag()}"
        )
        assert idle(forced=True), f"an exited command's tree read busy: {diag()}"

        # 3. The agent image with no launcher above it: a human who typed the
        #    agent at the prompt.
        typed = run(
            "send-keys",
            "-t",
            name,
            f"& {_ps_quote(str(stand_in))} -n 20 127.0.0.1",
            "Enter",
        )
        assert typed.returncode == 0, f"send-keys failed: {typed.stderr!r}"
        assert _wait_until(lambda: "claude.exe" in pane_images(), timeout=30), (
            f"the stand-in agent never started: {diag()}"
        )
        assert not idle(), f"a pane running the agent image read idle: {diag()}"
        assert not idle(forced=True), (
            f"the agent image under the pane's shell read idle: {diag()}"
        )
        assert _wait_until(idle, timeout=45), (
            f"the pane never read idle after the agent exited: {diag()}"
        )
    finally:
        release.touch()
        # Unconditional: a new-session that failed or timed out may still have
        # left a server up. Only ever targets our own -L name.
        psmux.kill_server(name, psmux=binary)

    assert _wait_until(lambda: not psmux.has_session(name), timeout=5), (
        f"cleanup left psmux session {name!r} alive"
    )


# --- idle_sessions' console stage against a REAL orphan ------------------------
#
# The tree walk above cannot see a process that left the pane's process tree
# but still shares the pane's CONSOLE (`start /b` from a batch that then
# exits). Anything typed into the pane can land in such an orphan, so
# idle_sessions' last stage reads the console's client list. This proves the
# reading against a real pane: at rest the console holds only the pane's
# subtree; with a detached orphan on it the pane is NOT idle (either way the
# foreground reads); with the orphan gone it is idle again. Same posture as the
# test above: a private -L socket, tmp_path only, and the only process killed
# is the orphan, found by its unique image name.


def test_real_pane_with_an_orphan_on_its_console_is_not_idle(tmp_path):
    from magent import log
    from magent.platform.windows import _ps_quote
    from magent.procs import console_clients, process_tree, snapshot_processes

    binary = psmux.find_psmux()
    assert binary is not None
    ping = shutil.which("ping")
    assert ping is not None

    unique = uuid.uuid4().hex[:12]
    name = f"mdrl-con-{unique}"
    workdir = tmp_path / f"cwd-{unique}"
    workdir.mkdir()
    orphan = tmp_path / f"orphan{unique}.exe"
    shutil.copyfile(ping, orphan)
    spawn = tmp_path / "spawn.cmd"
    spawn.write_text(
        f'@start "" /b "%~dp0orphan{unique}.exe" -n 40 127.0.0.1 >nul\n',
        encoding="utf-8",
    )

    def run(*args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            [binary, "-L", name, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )

    def pane_pid() -> int | None:
        return psmux.pane_pids([name], psmux=binary).get(name)

    def pane_images() -> list[str]:
        # Image STEMS (lower-case, no ".exe") via the public image_stem, so the
        # shell reads "pwsh"/"powershell", not "pwsh.exe" -- the spelling the
        # rest of the codebase compares image names in.
        pid = pane_pid()
        tree = process_tree(pid, snapshot_processes() or []) if pid else None
        return [psmux.image_stem(image) for image, _pid, _ppid in tree or []]

    def idle(forced: bool = False) -> bool:
        fg = {name: "pwsh"} if forced else None
        return name in psmux.idle_sessions([name], psmux=binary, foreground=fg)

    def orphan_pid() -> int | None:
        for image, pid, _ppid in snapshot_processes() or []:
            if image.lower() == orphan.name.lower():
                return pid
        return None

    def kill_orphan() -> None:
        op = orphan_pid()
        if op:
            subprocess.run(
                ["taskkill", "/PID", str(op), "/F"], capture_output=True, check=False
            )

    try:
        new = run(
            "new-session", "-d", "-s", name, "-c", str(workdir), env=psmux.child_env()
        )
        assert new.returncode == 0, f"new-session failed: {new.stderr!r}"
        assert _wait_until(lambda: psmux.has_session(name), timeout=10)

        # At rest the console holds only the pane's own subtree.
        assert _wait_until(idle, timeout=30), "a resting pane never read idle"
        pid = pane_pid()
        assert pid is not None
        clients = console_clients([pid])[pid]
        assert clients is not None and pid in clients

        # Spawn a detached orphan that shares the pane's CONSOLE but sits
        # OUTSIDE its process tree (start /b through a batch).
        typed = run("send-keys", "-t", name, f"& {_ps_quote(str(spawn))}", "Enter")
        assert typed.returncode == 0, f"send-keys failed: {typed.stderr!r}"
        assert _wait_until(lambda: orphan_pid() is not None, timeout=30), (
            "the orphan never started"
        )
        assert _wait_until(
            lambda: (
                pane_images() in (["pwsh"], ["powershell"]) and orphan_pid() is not None
            ),
            timeout=15,
        ), "the batch never returned the pane to its shell"

        # The pane's TREE is just its shell, but the console has the orphan:
        # not idle either way, and the orphan pid is among the clients.
        assert not idle(), "a pane whose console holds an orphan read idle"
        assert not idle(forced=True), "the forced read ignored the console orphan"
        op = orphan_pid()
        assert op is not None and op in (console_clients([pid])[pid] or frozenset())
        # ...and it was the CONSOLE stage that said no, naming the orphan (its
        # warning lands in conftest's tmp_path LOG_DIR, never the real one).
        veto_log = log.LOG_DIR / "launch.log"
        assert str(op) in veto_log.read_text(encoding="utf-8")

        # Kill the orphan and the pane returns to idle.
        kill_orphan()
        assert _wait_until(idle, timeout=30), (
            "the pane never read idle after the orphan died"
        )
    finally:
        kill_orphan()
        psmux.kill_server(name, psmux=binary)

    assert _wait_until(lambda: not psmux.has_session(name), timeout=5)


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
