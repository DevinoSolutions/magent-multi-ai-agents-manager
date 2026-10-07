"""Where the daemon-lifecycle fixes meet.

Four fixes landed on the same few seams at once: the pre-boot pid-file guard
(a reboot recycles pid numbers), the Session-0 daemon seams (an ssh login must
not plant survivors where no desktop can see them), serve's attention
supervisor, and serve's wedged-listener replacement. Each was proven alone.
These pins hold the combinations -- the cases neither branch could see,
because the other half did not exist yet on it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from magent import procs
from magent.cli import attention_cmd
from magent.cli import status as status_mod
from tests.conftest import FakePlatform

DESKTOP = 1


def _pre_boot(path: Path) -> None:
    """Stamp a pid file as written long before a boot at t=5000."""
    os.utime(path, (1000.0, 1000.0))


class TestAPreBootPidFileIsStaleEvenWhenItsPidIsUnopenable:
    """The pre-boot guard and the keep-the-evidence rule, together.

    Session-0 half: a pid that is alive but cannot be opened (a daemon an ssh
    login started, full admin token) keeps its pid file -- it is the only
    record naming that daemon. Pre-boot half: a pid file older than the boot
    names nothing, whatever the pid is doing now. When both hold, the boot
    wins: the file predates every process now alive, so an unopenable process
    wearing that number is a recycled pid (a SYSTEM service, say), not ours.
    Kept, it would be named by the stranded-daemon diagnostic forever.
    """

    def test_attention_clears_a_pre_boot_file_whose_pid_is_unopenable(
        self, monkeypatch, tmp_path
    ):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text("70")
        _pre_boot(pid_file)
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr(attention_cmd, "pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)

        assert attention_cmd.daemon_pid() is None
        assert not pid_file.exists()

    def test_attention_keeps_a_post_boot_file_whose_pid_is_unopenable(
        self, monkeypatch, tmp_path
    ):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text("70")
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr(attention_cmd, "pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 1000.0)

        assert attention_cmd.daemon_pid() is None
        assert pid_file.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
    def test_the_listener_clears_a_pre_boot_file_whose_pid_is_unopenable(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        pid_file = tmp_path / "hotkey.pid"
        pid_file.write_text("70")
        _pre_boot(pid_file)
        monkeypatch.setattr(hotkey, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)

        assert hotkey.listener_pid() is None
        assert not pid_file.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
    def test_the_listener_keeps_a_post_boot_file_whose_pid_is_unopenable(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        pid_file = tmp_path / "hotkey.pid"
        pid_file.write_text("70")
        monkeypatch.setattr(hotkey, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 1000.0)

        assert hotkey.listener_pid() is None
        assert pid_file.exists()


class TestTheStrandedDaemonDiagnosticIgnoresPreBootPidFiles:
    """``status.session0_daemons`` reads the pid files raw (it must not clear
    them: it is a read-only diagnostic). A file older than the boot cannot
    name a live daemon, and Session 0 is where every service a recycled pid
    could now name lives -- including a python-hosted one."""

    @pytest.fixture(autouse=True)
    def _world(self, monkeypatch, tmp_path):
        table: dict[int, tuple[str, int]] = {}
        monkeypatch.setattr(procs, "active_console_session_id", lambda: DESKTOP)
        monkeypatch.setattr(
            procs,
            "snapshot_processes",
            lambda: [(image, pid, 4) for pid, (image, _sid) in table.items()],
        )
        monkeypatch.setattr(
            procs, "session_id_of", lambda pid: table[pid][1] if pid in table else None
        )
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")
        plat = FakePlatform(supports_hotkey=False)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)
        self.table = table
        self.magent_dir = Path.home() / ".magent"
        self.magent_dir.mkdir(parents=True, exist_ok=True)

    def test_a_pre_boot_server_pid_file_is_not_a_stranded_server(self):
        self.table[70] = ("python.exe", 0)
        pid_file = self.magent_dir / "upload_server-8034.pid"
        pid_file.write_text("70")
        _pre_boot(pid_file)

        assert status_mod.session0_daemons() == []
        assert pid_file.exists()  # a diagnostic reads; it never clears

    def test_a_pre_boot_attention_pid_file_is_not_a_stranded_daemon(self):
        self.table[80] = ("python.exe", 0)
        attention_cmd._PID_PATH.write_text("80")
        _pre_boot(attention_cmd._PID_PATH)

        assert status_mod.session0_daemons() == []

    def test_a_post_boot_one_still_is(self):
        self.table[70] = ("python.exe", 0)
        (self.magent_dir / "upload_server-8034.pid").write_text("70")

        assert status_mod.session0_daemons() == [("upload server :8034", 70)]


class _Spawns:
    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.argvs.append(argv)
        return 4242


class TestServesAttentionSupervisorNeverRevivesFromSessionZero:
    """serve's attention supervisor is a daemon spawn seam like every other,
    so it asks ``launch.session0_block``. ``attention_watchdog`` already
    declines to build it in Session 0; the supervisor refuses on its own too,
    so no caller can route around the seam."""

    @pytest.fixture(autouse=True)
    def _crashed_daemon(self, monkeypatch):
        # No live daemon, and a heartbeat left behind: exactly what a revive
        # acts on. Renderers exist, so only the session can stop it.
        monkeypatch.setattr(attention_cmd, "daemon_pid", lambda: None)
        monkeypatch.setattr("magent.log.heartbeat_age", lambda name: 60.0)
        monkeypatch.setattr(
            attention_cmd.AttentionDaemonSupervisor, "_has_work", lambda self: True
        )
        self.spawns = _Spawns()
        monkeypatch.setattr("magent.launch.spawn_detached", self.spawns)

    def _session_zero(self, monkeypatch, policy: str) -> FakePlatform:
        # A desktop to hand off TO exists: a seam still never hands off (it is
        # not the command the user typed), it only refuses.
        plat = FakePlatform(interactive_session=False, supports_handoff=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        monkeypatch.setenv("MAGENT_SESSION0_POLICY", policy)
        monkeypatch.setattr("magent.env._cached_env", None)
        return plat

    @pytest.mark.parametrize("policy", ["refuse", "handoff"])
    def test_a_session_zero_serve_never_revives_the_daemon(
        self, monkeypatch, caplog, policy
    ):
        plat = self._session_zero(monkeypatch, policy)
        sup = attention_cmd.AttentionDaemonSupervisor(None, now=lambda: 100.0)

        with caplog.at_level("WARNING", logger="magent.attention"):
            assert sup.tick() is False

        assert self.spawns.argvs == []
        assert plat.handoffs == []
        assert "attention daemon" in caplog.text
        assert "Session 0" in caplog.text

    def test_the_refusal_is_said_once_a_cooldown(self, monkeypatch, caplog):
        self._session_zero(monkeypatch, "refuse")
        clock = [100.0]
        sup = attention_cmd.AttentionDaemonSupervisor(None, now=lambda: clock[0])

        with caplog.at_level("WARNING", logger="magent.attention"):
            sup.tick()
            clock[0] += 30.0
            sup.tick()
        assert caplog.text.count("refusing to start the attention daemon") == 1

        clock[0] += sup.cooldown_s
        with caplog.at_level("WARNING", logger="magent.attention"):
            sup.tick()
        assert caplog.text.count("refusing to start the attention daemon") == 2
        assert self.spawns.argvs == []

    def test_allow_on_a_headless_host_revives_as_before(self, monkeypatch):
        self._session_zero(monkeypatch, "allow")
        sup = attention_cmd.AttentionDaemonSupervisor(None, now=lambda: 100.0)

        assert sup.tick() is True
        assert self.spawns.argvs and self.spawns.argvs[0][-2:] == ["attention", "-d"]

    def test_the_desktop_revives_as_before(self, monkeypatch):
        plat = FakePlatform(interactive_session=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        sup = attention_cmd.AttentionDaemonSupervisor(None, now=lambda: 100.0)

        assert sup.tick() is True
        assert len(self.spawns.argvs) == 1


class TestTheWedgedListenerReplacementRespectsSessionZero:
    """``ensure_hotkey_listener`` with a watch can END a process before it
    starts one. In Session 0 the start is refused (``start_hotkey_listener``),
    so a replacement there would kill the desktop's listener and put nothing
    back. The seam refuses first: no pid read, no retire, no spawn."""

    def test_nothing_is_read_retired_or_spawned(self, monkeypatch, caplog):
        import types

        from magent import launch

        plat = FakePlatform(
            interactive_session=False, supports_handoff=True, supports_hotkey=True
        )
        monkeypatch.setattr("magent.launch.get_platform", lambda: plat)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        monkeypatch.setenv("MAGENT_SESSION0_POLICY", "handoff")
        monkeypatch.setattr("magent.env._cached_env", None)
        seen: list[str] = []
        fake_hotkey = types.ModuleType("magent.hotkey")
        fake_hotkey.listener_pid = lambda: seen.append("pid") or 501
        fake_hotkey.listener_manifest = lambda: None
        monkeypatch.setitem(sys.modules, "magent.hotkey", fake_hotkey)
        monkeypatch.setattr(
            "magent.launch.retire_wedged_listener",
            lambda *a, **k: seen.append("retire") or True,
        )
        monkeypatch.setattr(
            "magent.launch.start_hotkey_listener",
            lambda *a, **k: seen.append("start") or 4242,
        )

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            got = launch.ensure_hotkey_listener(
                "http://127.0.0.1:8034", watch=launch.ListenerWatch()
            )

        assert got is None
        assert seen == []
        assert "refusing to start the Alt+V listener" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
class TestTheWedgedListenerReplacementNeverSeesARecycledPid:
    """The replacement reads the pid through ``hotkey.listener_pid``, the
    pre-boot-guarded reader. After a reboot the old listener's pid file and
    heartbeat both linger, and the pid can come back on a python process that
    looks, to every other check, like a wedged listener that stopped pulsing.
    It must never be considered, let alone ended: the file predates the boot."""

    def test_a_pre_boot_pid_is_never_retired(self, monkeypatch, tmp_path):
        from magent import hotkey, launch
        from magent.procs import ProcessIdentity

        pid_file = tmp_path / "hotkey.pid"
        pid_file.write_text(str(os.getpid()))  # alive, as a recycled pid is
        _pre_boot(pid_file)
        monkeypatch.setattr(hotkey, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)
        # A heartbeat silent far past the grace, and an identity (python, born
        # before that pulse) that would PROVE a wedge if the recycled pid ever
        # got that far.
        last_pulse = time.time() - 10 * launch.WEDGED_LISTENER_GRACE_S
        born = last_pulse - 600
        monkeypatch.setattr("magent.launch.heartbeat_mtime", lambda name: last_pulse)
        monkeypatch.setattr(
            "magent.launch.process_identity",
            lambda pid: ProcessIdentity(
                "python.exe", int((born + 11_644_473_600) * 10_000_000)
            ),
        )

        def _kill(pid, identity):
            raise AssertionError(f"ended pid {pid} from a pre-boot pid file")

        monkeypatch.setattr("magent.launch.terminate_verified", _kill)
        starts: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            "magent.launch.start_hotkey_listener",
            lambda url, ssh_host=None: starts.append((url, ssh_host)) or 4242,
        )
        watch = launch.ListenerWatch()

        for _ in range(3):  # past the two-tick confirm
            launch.ensure_hotkey_listener("http://127.0.0.1:8034", watch=watch)

        # No listener, so a fresh one at the supervisor's own URL, every tick
        # (the real start_hotkey_listener would register it after the first).
        assert starts == [("http://127.0.0.1:8034", None)] * 3
        assert not pid_file.exists()


class TestServeCannotReviveAttentionMidDown:
    """``down --all`` stops attention first, and ``stop_daemon`` withdraws the
    heartbeat before the kill -- the marker serve's supervisor revives on. But
    the daemon's own heartbeat thread keeps pulsing until the kill lands, so a
    pulse can slip in after the withdrawal; a serve tick that then runs between
    the kill and the second clear sees exactly a crash (no live pid, heartbeat
    present) and starts a daemon the teardown never learns of -- which then
    revives the serve the teardown is about to stop. A heartbeat still fresh
    (HEARTBEAT_MAX_AGE, the window status reads "on" by) proves nothing dead,
    so it is never acted on; a real crash is still revived, a tick or two
    later."""

    @pytest.fixture(autouse=True)
    def _world(self, monkeypatch, tmp_path):
        self.pid_file = tmp_path / "attention.pid"
        self.pid_file.write_text("4321")
        monkeypatch.setattr(attention_cmd, "_PID_PATH", self.pid_file)
        self.alive = {4321}
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda p: p in self.alive)
        monkeypatch.setattr(attention_cmd, "pid_alive", lambda p: p in self.alive)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda p: p not in self.alive)
        monkeypatch.setattr(
            attention_cmd.AttentionDaemonSupervisor, "_has_work", lambda self: True
        )
        plat = FakePlatform(interactive_session=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        self.spawns = _Spawns()
        monkeypatch.setattr("magent.launch.spawn_detached", self.spawns)

    def test_a_tick_landing_inside_stop_daemon_revives_nothing(self, monkeypatch):
        from magent import log

        sup = attention_cmd.AttentionDaemonSupervisor(None)
        during_kill: list[bool] = []

        def _kill(*_a, **_k):
            # The daemon's heartbeat thread pulsed after the withdrawal...
            log.write_heartbeat(attention_cmd.HEARTBEAT_NAME)
            self.alive.discard(4321)
            # ...and serve's supervisor ticks before stop_daemon clears again.
            during_kill.append(sup.tick())
            return "terminated"

        monkeypatch.setattr("magent.pidfile.terminate_pid", _kill)
        log.write_heartbeat(attention_cmd.HEARTBEAT_NAME)

        assert attention_cmd.stop_daemon() is True
        assert during_kill == [False]
        assert self.spawns.argvs == []
        assert log.heartbeat_age(attention_cmd.HEARTBEAT_NAME) is None

    def test_a_stale_pulse_is_a_crash_and_is_revived(self):
        from magent import log

        self.alive.clear()
        log.write_heartbeat(attention_cmd.HEARTBEAT_NAME)
        hb = log.HEARTBEAT_DIR / f"{attention_cmd.HEARTBEAT_NAME}.heartbeat"
        stamp = time.time() - log.HEARTBEAT_MAX_AGE - 1
        os.utime(hb, (stamp, stamp))

        assert attention_cmd.AttentionDaemonSupervisor(None).tick() is True
        assert len(self.spawns.argvs) == 1

    def test_a_daemon_still_pulsing_where_this_desktop_cannot_see_it_is_left_alone(
        self,
    ):
        # A daemon an ssh login started in Session 0 cannot be opened from the
        # desktop, so daemon_pid() is None -- while it keeps pulsing the shared
        # heartbeat. Between two pulses the age runs past one interval; a
        # revive there is a SECOND daemon beside it. Only a heartbeat stale by
        # status's own definition (HEARTBEAT_MAX_AGE) is a dead daemon.
        from magent import log

        self.alive.clear()
        log.write_heartbeat(attention_cmd.HEARTBEAT_NAME)
        hb = log.HEARTBEAT_DIR / f"{attention_cmd.HEARTBEAT_NAME}.heartbeat"
        stamp = time.time() - log.HEARTBEAT_INTERVAL - 2
        os.utime(hb, (stamp, stamp))

        assert attention_cmd.AttentionDaemonSupervisor(None).tick() is False
        assert self.spawns.argvs == []
