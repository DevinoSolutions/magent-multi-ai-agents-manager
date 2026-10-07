"""Finding magent's OWN daemons stranded in logon Session 0.

The spawn seams now refuse (test_session0_daemons.py); this is the other half,
the one `psmux.session0_server_pids` is for psmux: a serve, Alt+V listener or
attention daemon an older magent -- or a foreground command typed over ssh --
already left in Session 0. A Session-0 serve holds the loopback port this
desktop's Alt+V needs, and none of the three can be seen or stopped from here.

Identity comes from magent's own pid files (the only record of which process
is which daemon), liveness and session from the one process snapshot plus the
handle-free per-pid session id, and the whole question is asked only while a
desktop exists. Every id here comes from a fake; nothing touches a real
process's session.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from magent import procs
from magent.cli import attention_cmd
from magent.cli import status as status_mod
from tests.conftest import FakePlatform

DESKTOP = 1


@pytest.fixture
def machine(monkeypatch):
    """A fake process table: ``{pid: (image, session)}``, console on session 1."""
    table: dict[int, tuple[str, int]] = {}
    calls = {"snapshot": 0}

    def _snapshot():
        calls["snapshot"] += 1
        return [(image, pid, 4) for pid, (image, _sid) in table.items()]

    monkeypatch.setattr(procs, "active_console_session_id", lambda: DESKTOP)
    monkeypatch.setattr(procs, "snapshot_processes", _snapshot)
    monkeypatch.setattr(
        procs, "session_id_of", lambda pid: table[pid][1] if pid in table else None
    )
    return table, calls


def _magent_dir() -> Path:
    d = Path.home() / ".magent"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSessionZeroResidents:
    def test_a_session_zero_pid_is_reported_with_its_image(self, machine):
        table, _ = machine
        table[70] = ("python.exe", 0)

        assert procs.session0_residents([70]) == {70: "python.exe"}

    def test_a_desktop_pid_is_not(self, machine):
        table, _ = machine
        table[70] = ("python.exe", DESKTOP)

        assert procs.session0_residents([70]) == {}

    def test_a_dead_pid_is_not(self, machine):
        assert procs.session0_residents([70]) == {}

    @pytest.mark.parametrize("console", [None, *procs.NO_CONSOLE_SESSION])
    def test_with_no_desktop_the_question_is_not_asked(
        self, machine, monkeypatch, console
    ):
        # A headless host runs its daemons in Session 0 on purpose
        # (MAGENT_SESSION0_POLICY=allow); there is no desktop they are missing
        # from. The console id is read for that question ONLY -- never to pick
        # a session.
        table, calls = machine
        table[70] = ("python.exe", 0)
        monkeypatch.setattr(procs, "active_console_session_id", lambda: console)

        assert procs.session0_residents([70]) == {}
        assert calls["snapshot"] == 0

    def test_an_unreadable_snapshot_invents_nothing(self, machine, monkeypatch):
        table, _ = machine
        table[70] = ("python.exe", 0)
        monkeypatch.setattr(procs, "snapshot_processes", lambda: None)

        assert procs.session0_residents([70]) == {}

    def test_no_pids_costs_no_snapshot(self, machine):
        _, calls = machine

        assert procs.session0_residents([]) == {}
        assert calls["snapshot"] == 0


class TestPidGone:
    """``pid_alive`` is False for a process this user cannot OPEN -- and a
    daemon an ssh login started in Session 0 (full admin token) is exactly
    that. Deleting its pid file erased the only record it exists."""

    def test_an_openable_live_pid_is_not_gone(self, monkeypatch):
        monkeypatch.setattr(procs, "pid_alive", lambda pid: True)
        monkeypatch.setattr(procs, "session_id_of", lambda pid: DESKTOP)
        assert procs.pid_gone(70) is False

    def test_a_live_but_unopenable_pid_is_not_gone(self, monkeypatch):
        monkeypatch.setattr(procs, "pid_alive", lambda pid: False)
        monkeypatch.setattr(procs, "session_id_of", lambda pid: 0)
        assert procs.pid_gone(70) is False

    def test_a_pid_with_no_process_is_gone(self, monkeypatch):
        monkeypatch.setattr(procs, "pid_alive", lambda pid: False)
        monkeypatch.setattr(procs, "session_id_of", lambda pid: None)
        assert procs.pid_gone(70) is True


class TestThePidReadersKeepTheEvidence:
    def test_attention_keeps_a_live_session_zero_daemons_pid_file(
        self, monkeypatch, tmp_path
    ):
        pid_file = tmp_path / "attention.pid"
        pid_file.write_text("70")
        monkeypatch.setattr(attention_cmd, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr(attention_cmd, "pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)

        # Not OURS to use (this desktop cannot see or stop it), so None -- but
        # the file is the diagnostic's only way to name it.
        assert attention_cmd.daemon_pid() is None
        assert pid_file.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
    def test_the_listener_keeps_a_live_session_zero_listeners_pid_file(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        pid_file = tmp_path / "hotkey.pid"
        pid_file.write_text("70")
        monkeypatch.setattr(hotkey, "_PID_PATH", pid_file)
        monkeypatch.setattr("magent.pidfile.pid_alive", lambda pid: False)
        monkeypatch.setattr("magent.pidfile.pid_gone", lambda pid: False)

        assert hotkey.listener_pid() is None
        assert pid_file.exists()


class TestSessionZeroDaemons:
    @pytest.fixture(autouse=True)
    def _world(self, monkeypatch, tmp_path):
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")
        plat = FakePlatform(supports_hotkey=False)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)

    def test_a_stranded_server_and_daemon_are_named(self, machine):
        table, _ = machine
        table[70] = ("python.exe", 0)
        table[80] = ("pythonw.exe", 0)
        (_magent_dir() / "upload_server-8034.pid").write_text("70")
        attention_cmd._PID_PATH.write_text("80")

        assert status_mod.session0_daemons() == [
            ("upload server :8034", 70),
            ("attention daemon", 80),
        ]

    def test_daemons_on_the_desktop_are_not(self, machine):
        table, _ = machine
        table[70] = ("python.exe", DESKTOP)
        (_magent_dir() / "upload_server-8034.pid").write_text("70")

        assert status_mod.session0_daemons() == []

    def test_a_pid_recycled_onto_a_service_is_not_blamed_on_magent(self, machine):
        # A pid file outlives its process, and Session 0 is where every
        # service lives -- a stale file must not accuse svchost.
        table, _ = machine
        table[70] = ("svchost.exe", 0)
        (_magent_dir() / "upload_server-8034.pid").write_text("70")

        assert status_mod.session0_daemons() == []

    def test_no_pid_files_costs_no_snapshot(self, machine):
        _, calls = machine

        assert status_mod.session0_daemons() == []
        assert calls["snapshot"] == 0

    @pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
    def test_a_stranded_listener_is_named(self, machine, monkeypatch, tmp_path):
        from magent import hotkey

        table, _ = machine
        table[90] = ("pythonw.exe", 0)
        monkeypatch.setattr(hotkey, "_PID_PATH", tmp_path / "hotkey.pid")
        hotkey._PID_PATH.write_text("90")
        plat = FakePlatform(supports_hotkey=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)

        assert status_mod.session0_daemons() == [("Alt+V listener", 90)]


class TestTheMessage:
    def test_it_names_each_daemon_and_the_repair(self):
        msg = status_mod.session0_daemons_message(
            [("upload server :8034", 70), ("attention daemon", 80)]
        )

        assert "upload server :8034 (pid 70)" in msg
        assert "attention daemon (pid 80)" in msg
        assert "logon Session 0" in msg
        assert "elevated shell" in msg
        assert "taskkill /F /PID 70 /PID 80" in msg
        assert "magent serve --ensure" in msg
        assert "magent attention -d" in msg

    def test_it_only_suggests_restarting_what_was_stranded(self):
        msg = status_mod.session0_daemons_message([("upload server :8034", 70)])

        assert "magent serve --ensure" in msg
        assert "magent attention -d" not in msg
