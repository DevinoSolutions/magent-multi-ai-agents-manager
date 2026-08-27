"""The psmux priority sweep: `procs.boost_above_normal` (the primitive),
`psmux.boost_priority` (the one seam), and the three owners that call it.

WHY THIS FEATURE EXISTS, measured: typing into a magent pane lagged badly under
CPU load while ordinary Windows textboxes stayed snappy. A keystroke's echo
crosses Windows Terminal -> the psmux attach client -> a named pipe -> the psmux
server -> ConPTY and back, with the client repainting off a ~10ms poll; every
one of those psmux processes runs at NORMAL priority (169 of them live on the
reporting box, not one above normal), none of them gets the foreground-window
boost because none of them owns a window, and psmux itself never calls
SetPriorityClass anywhere. They are I/O-bound -- blocked on a pipe, not
competing for CPU -- so raising them costs the compute fleet nothing and wins
them the scheduler the moment a key arrives.

WHY A SWEEP AND NOT A SPAWN FLAG, which is what makes this a background job at
all: a Windows priority class is NOT inherited by grandchildren, and magent
never CreateProcess-es the psmux SERVER -- the one-shot psmux client forks it.
There is no spawn for a flag to ride on, so the only shape that can work is an
idempotent sweep over the live process list.

NOTHING HERE TOUCHES A REAL PROCESS. Every test drives the injected
enumerator/setter seams. The real ctypes primitives are exercised in
`test_procs.py::TestRaisePriorityAboveNormal`, and there only against a child
that test spawned itself -- the live fleet is never enumerated-and-modified.
"""

from __future__ import annotations

import sys

import pytest

from magent import procs, psmux


class _Fleet:
    """A fake machine: image name -> pid -> priority class."""

    def __init__(self, processes: dict[str, list[tuple[int, int]]]) -> None:
        """``processes`` maps an image name to its (pid, current class) pairs."""
        self.classes: dict[int, int] = {}
        self.names: dict[str, list[int]] = {}
        for name, entries in processes.items():
            self.names[name] = [pid for pid, _cls in entries]
            for pid, cls in entries:
                self.classes[pid] = cls
        self.touched: list[int] = []
        self.opened: list[int] = []

    def list_pids(self, names) -> list[int]:
        wanted = {n.casefold() for n in names}
        return [
            pid
            for name, pids in self.names.items()
            if name.casefold() in wanted
            for pid in pids
        ]

    def raise_priority(self, pid: int) -> bool:
        """The real primitive's contract, in Python: raise only from at-or-below
        NORMAL, report whether this call changed anything."""
        self.opened.append(pid)
        current = self.classes[pid]
        if current not in (
            procs.NORMAL_PRIORITY_CLASS,
            procs.BELOW_NORMAL_PRIORITY_CLASS,
            procs.IDLE_PRIORITY_CLASS,
        ):
            return False
        self.classes[pid] = procs.ABOVE_NORMAL_PRIORITY_CLASS
        self.touched.append(pid)
        return True


class TestTheSweepOnlyTouchesPsmux:
    def test_only_matching_image_names_are_opened_at_all(self):
        fleet = _Fleet(
            {
                "psmux.exe": [(10, procs.NORMAL_PRIORITY_CLASS)],
                "chrome.exe": [(20, procs.NORMAL_PRIORITY_CLASS)],
                "python.exe": [(30, procs.NORMAL_PRIORITY_CLASS)],
            }
        )

        boosted = procs.boost_above_normal(
            {"psmux.exe"},
            list_pids=fleet.list_pids,
            raise_priority=fleet.raise_priority,
        )

        assert boosted == 1
        # Not merely "not boosted": a process magent knows nothing about must
        # never even be OPENED by this sweep.
        assert fleet.opened == [10]
        assert fleet.classes[20] == procs.NORMAL_PRIORITY_CLASS
        assert fleet.classes[30] == procs.NORMAL_PRIORITY_CLASS

    def test_image_names_match_case_insensitively(self, monkeypatch):
        # The reporting box's binary is `psmux.EXE` on disk while the release
        # zip ships `psmux.exe`; Windows filenames are case-insensitive and the
        # match has to be too, or the whole fleet is invisible. Driven through
        # the REAL filter, with only the OS snapshot substituted.
        monkeypatch.setattr(
            procs,
            "snapshot_processes",
            lambda: [("PSMUX.EXE", 10), ("pSmUx.ExE", 11), ("notepad.exe", 12)],
        )

        assert procs.pids_by_image_name({"psmux.exe"}) == [10, 11]
        assert procs.pids_by_image_name({"PSMUX.EXE"}) == [10, 11]

    def test_a_failed_snapshot_sweeps_nothing_rather_than_guessing(self, monkeypatch):
        monkeypatch.setattr(procs, "snapshot_processes", lambda: None)

        assert procs.pids_by_image_name({"psmux.exe"}) == []

    def test_every_matching_pid_is_swept_not_just_the_first(self):
        fleet = _Fleet(
            {
                "psmux.exe": [
                    (1, procs.NORMAL_PRIORITY_CLASS),
                    (2, procs.NORMAL_PRIORITY_CLASS),
                    (3, procs.IDLE_PRIORITY_CLASS),
                ]
            }
        )

        assert (
            procs.boost_above_normal(
                {"psmux.exe"},
                list_pids=fleet.list_pids,
                raise_priority=fleet.raise_priority,
            )
            == 3
        )
        assert fleet.touched == [1, 2, 3]


class TestItNeverDowngrades:
    """A raise, never a change. Somebody put a HIGH process there on purpose,
    and a sweep that ran every 30 seconds and quietly demoted it would be a
    background job overruling a foreground decision."""

    @pytest.mark.parametrize(
        ("start", "expected_class", "raised"),
        [
            (procs.NORMAL_PRIORITY_CLASS, procs.ABOVE_NORMAL_PRIORITY_CLASS, 1),
            (procs.BELOW_NORMAL_PRIORITY_CLASS, procs.ABOVE_NORMAL_PRIORITY_CLASS, 1),
            (procs.IDLE_PRIORITY_CLASS, procs.ABOVE_NORMAL_PRIORITY_CLASS, 1),
            # Already there: nothing to do, and it must not be counted as work.
            (procs.ABOVE_NORMAL_PRIORITY_CLASS, procs.ABOVE_NORMAL_PRIORITY_CLASS, 0),
            (0x00000080, 0x00000080, 0),  # HIGH_PRIORITY_CLASS
            (0x00000100, 0x00000100, 0),  # REALTIME_PRIORITY_CLASS
        ],
    )
    def test_a_process_is_only_ever_raised(self, start, expected_class, raised):
        fleet = _Fleet({"psmux.exe": [(10, start)]})

        got = procs.boost_above_normal(
            {"psmux.exe"},
            list_pids=fleet.list_pids,
            raise_priority=fleet.raise_priority,
        )

        assert got == raised
        assert fleet.classes[10] == expected_class

    def test_a_second_sweep_is_a_no_op(self):
        # Idempotence is what makes three owners and a 30s cadence safe.
        fleet = _Fleet({"psmux.exe": [(10, procs.NORMAL_PRIORITY_CLASS)]})
        kwargs = {
            "list_pids": fleet.list_pids,
            "raise_priority": fleet.raise_priority,
        }

        assert procs.boost_above_normal({"psmux.exe"}, **kwargs) == 1
        assert procs.boost_above_normal({"psmux.exe"}, **kwargs) == 0
        assert fleet.touched == [10]


class TestPerPidFailuresAreTolerated:
    """A live fleet churns: a pid from the snapshot can be gone microseconds
    later, and another user's process is simply refused. Neither is an error,
    and neither may cut the sweep short -- an aborted sweep boosts an arbitrary
    prefix of the fleet, which is worse than not sweeping."""

    def test_a_pid_that_refuses_does_not_stop_the_rest(self):
        seen: list[int] = []

        def flaky(pid: int) -> bool:
            seen.append(pid)
            return pid != 2  # pid 2: access denied / already gone

        assert (
            procs.boost_above_normal(
                {"psmux.exe"},
                list_pids=lambda _n: [1, 2, 3],
                raise_priority=flaky,
            )
            == 2
        )
        assert seen == [1, 2, 3]

    def test_a_pid_that_raises_oserror_does_not_stop_the_rest(self):
        def exploding(pid: int) -> bool:
            if pid == 2:
                raise OSError("the process has exited")
            return True

        assert (
            procs.boost_above_normal(
                {"psmux.exe"},
                list_pids=lambda _n: [1, 2, 3],
                raise_priority=exploding,
            )
            == 2
        )

    def test_an_empty_machine_is_zero_not_an_error(self):
        assert procs.boost_above_normal({"psmux.exe"}, list_pids=lambda _n: []) == 0


class TestOffWindowsItIsANoOp:
    """Priority classes are a Windows concept. Off Windows the primitives must
    answer emptily rather than guess, exactly as ``count_processes`` does."""

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_the_enumerator_finds_nothing(self):
        assert procs.pids_by_image_name({"psmux.exe"}) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_the_setter_reports_no_change(self):
        import os

        assert procs.raise_priority_above_normal(os.getpid()) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_the_seam_boosts_nothing_and_still_answers(self, monkeypatch):
        monkeypatch.setenv("MAGENT_PSMUX_BOOST", "1")
        monkeypatch.setattr("magent.env._cached_env", None)

        assert psmux.boost_priority() == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_the_snapshot_admits_it_cannot_look(self):
        # None, never []: "I could not look" and "nothing is running" are
        # different claims, and count_processes renders the difference.
        assert procs.snapshot_processes() is None


class TestTheImageNameSet:
    """Which image names ARE psmux, decided against the real release artifact
    rather than assumed."""

    def test_it_covers_both_names_the_release_zip_installs(self):
        # psmux-v3.3.6/3.3.8-windows-x64.zip both ship the SAME binary three
        # times: psmux.exe, pmux.exe and tmux.exe, all extracted side by side.
        # Which name a running server carries is whichever one was invoked.
        assert "psmux.exe" in psmux.PSMUX_IMAGE_NAMES
        assert "pmux.exe" in psmux.PSMUX_IMAGE_NAMES

    def test_tmux_is_deliberately_not_claimed(self):
        # `tmux.exe` is not psmux's name to claim: an MSYS2/Cygwin box can carry
        # an unrelated one, and this sweep must not re-prioritise a process
        # magent never launched and knows nothing about.
        assert "tmux.exe" not in psmux.PSMUX_IMAGE_NAMES

    def test_the_names_are_lowercase_so_the_match_is_well_defined(self):
        assert all(n == n.casefold() for n in psmux.PSMUX_IMAGE_NAMES)


class TestTheKillSwitch:
    """MAGENT_PSMUX_BOOST is the third member of the same test-isolation law as
    MAGENT_HOTKEY_SUPERVISOR / MAGENT_UPLOAD_SUPERVISOR, and the sharpest of the
    three: this sweep is the one thing in the product that reaches processes it
    did not spawn, by image name, which no HOME redirect can contain."""

    def _spy(self, monkeypatch) -> list[object]:
        calls: list[object] = []
        monkeypatch.setattr(
            "magent.procs.boost_above_normal",
            lambda names, **_kw: calls.append(names) or 3,
        )
        return calls

    def test_zero_means_the_primitive_is_never_reached(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.setenv("MAGENT_PSMUX_BOOST", "0")
        monkeypatch.setattr("magent.env._cached_env", None)

        assert psmux.boost_priority() == 0
        assert calls == []

    def test_it_is_on_by_default(self, monkeypatch):
        calls = self._spy(monkeypatch)
        monkeypatch.delenv("MAGENT_PSMUX_BOOST", raising=False)
        monkeypatch.setattr("magent.env._cached_env", None)

        assert psmux.boost_priority() == 3
        assert calls == [psmux.PSMUX_IMAGE_NAMES]

    def test_the_suite_wide_fixture_pins_it_off(self):
        # The law itself, asserted: tests/conftest.py sets it to 0 for every
        # tier, so no test that starts a real serve/daemon can re-prioritise
        # the developer's live fleet.
        monkeypatched = psmux.boost_enabled()
        assert monkeypatched is False

    def test_a_broken_environment_degrades_to_sweeping(self, monkeypatch, caplog):
        # A supervisor thread must never die of an env var it does not use --
        # the same degradation upload_server.supervision_enabled makes.
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "NOT-A-LEVEL")
        monkeypatch.setattr("magent.env._cached_env", None)

        with caplog.at_level("WARNING", logger="magent.launch"):
            assert psmux.boost_enabled() is True

        assert "psmux boost" in caplog.text


class TestTheThreeOwnersAllCallTheOneSeam:
    """Three owners because each covers a hole the others leave. The launch path
    boosts what it just created; the attention daemon re-sweeps on every poll
    (sessions created by `attach`, by `up`, or by hand hours later); and `serve`
    sweeps too because on a real box serve is always running while the attention
    daemon frequently is not."""

    def test_the_launch_path_boosts_after_bring_up(self, monkeypatch, tmp_path):
        from magent import config, launch
        from magent.platform import PsmuxWindowOpts

        calls: list[int] = []
        monkeypatch.setattr(psmux, "boost_priority", lambda: calls.append(1) or 1)
        monkeypatch.setattr(psmux, "launch_verified", lambda _p, _w: [])

        class _Plat:
            def attach_psmux(self, *_a, **_k) -> None:
                return None

            def supports_hotkey(self) -> bool:
                return False

        cfg = config.MagentConfig(
            projects=[config.ProjectConfig(path="api")],
            settings=config.Settings(upload_server=False),
        )
        result = launch._LaunchResult(
            targets=[],
            psmux_windows=[
                PsmuxWindowOpts(window_name="api", cwd=str(tmp_path), command="x")
            ],
            psmux_colors={},
        )

        launch._start_psmux_and_upload(
            _Plat(), cfg, launch.RunOpts(dry_run=False), result
        )

        assert calls == [1], "the bring-up must sweep the fleet it just created"

    def test_a_failing_sweep_never_breaks_the_bring_up(self, monkeypatch, tmp_path):
        from magent import config, launch
        from magent.platform import PsmuxWindowOpts

        def boom() -> int:
            raise OSError("kernel32 said no")

        monkeypatch.setattr(psmux, "boost_priority", boom)
        monkeypatch.setattr(psmux, "launch_verified", lambda _p, _w: [])

        class _Plat:
            def attach_psmux(self, *_a, **_k) -> None:
                return None

            def supports_hotkey(self) -> bool:
                return False

        cfg = config.MagentConfig(
            projects=[config.ProjectConfig(path="api")],
            settings=config.Settings(upload_server=False),
        )
        result = launch._LaunchResult(
            targets=[],
            psmux_windows=[
                PsmuxWindowOpts(window_name="api", cwd=str(tmp_path), command="x")
            ],
            psmux_colors={},
        )

        # Must not raise: the sessions are up either way.
        launch._start_psmux_and_upload(
            _Plat(), cfg, launch.RunOpts(dry_run=False), result
        )

    def test_the_attention_daemon_sweeps_every_tick(self, monkeypatch):
        from magent.cli import attention_cmd

        calls: list[int] = []
        monkeypatch.setattr(psmux, "boost_priority", lambda: calls.append(1) or 1)

        tick = attention_cmd._psmux_boost_tick()
        tick()
        tick()

        assert calls == [1, 1]

    def test_the_daemon_sweeps_even_with_no_upload_server_to_watch(self, monkeypatch):
        # The two watchdogs are independent: a config with uploadServer off
        # still has psmux panes somebody is typing into.
        from magent import config
        from magent.cli import attention_cmd

        calls: list[int] = []
        monkeypatch.setattr(psmux, "boost_priority", lambda: calls.append(1) or 1)
        cfg = config.MagentConfig(
            projects=[config.ProjectConfig(path="api")],
            settings=config.Settings(upload_server=False),
        )

        attention_cmd._daemon_tick(cfg, None)([])

        assert calls == [1]

    def test_a_failing_sweep_never_kills_the_daemon_loop(self, monkeypatch, caplog):
        from magent.cli import attention_cmd

        def boom() -> int:
            raise OSError("kernel32 said no")

        monkeypatch.setattr(psmux, "boost_priority", boom)
        tick = attention_cmd._psmux_boost_tick()

        with caplog.at_level("ERROR", logger="magent.attention"):
            tick()  # must not raise

        assert "psmux boost" in caplog.text

    @pytest.mark.skipif(sys.platform != "win32", reason="the sweep is win32-only")
    def test_serve_supervises_the_priority_too(self, monkeypatch):
        import threading

        from magent import upload_server

        calls: list[int] = []
        stop = threading.Event()

        def _boost() -> int:
            calls.append(1)
            stop.set()  # one pass, then let the loop exit
            return 1

        monkeypatch.setattr(psmux, "boost_priority", _boost)

        upload_server._supervise_psmux_priority(stop, interval=0.01)

        assert calls == [1]

    @pytest.mark.skipif(sys.platform != "win32", reason="the sweep is win32-only")
    def test_a_failing_sweep_never_takes_serve_down(self, monkeypatch, caplog):
        import threading

        from magent import upload_server

        stop = threading.Event()

        def boom() -> int:
            stop.set()
            raise OSError("kernel32 said no")

        monkeypatch.setattr(psmux, "boost_priority", boom)

        with caplog.at_level("ERROR", logger="magent.launch"):
            upload_server._supervise_psmux_priority(stop, interval=0.01)

        assert "psmux boost" in caplog.text

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX early return")
    def test_off_windows_serve_does_not_even_spin_a_loop(self, monkeypatch):
        import threading

        from magent import upload_server

        monkeypatch.setattr(
            psmux, "boost_priority", lambda: pytest.fail("swept off Windows")
        )

        # Returns immediately on a never-set event: no loop, no wait.
        upload_server._supervise_psmux_priority(threading.Event(), interval=999)

    def test_serve_starts_the_supervisor_thread(self, monkeypatch):
        """End of the wiring: run_server really spins this thread up."""
        import threading

        from magent import upload_server

        started: list[object] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            """Records WHAT run_server wanted to run, and runs none of it -- a
            real thread here would sweep the machine running the suite."""

            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded_target = target

            def start(self) -> None:
                started.append(self.recorded_target)

        monkeypatch.setattr(upload_server.threading, "Thread", _Recording)
        monkeypatch.setattr(upload_server, "_bind_addresses", lambda _h: ["127.0.0.1"])

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(upload_server, "_NoFqdnHTTPServer", _FakeServer)

        with pytest.raises(KeyboardInterrupt):
            upload_server.run_server(port=0)

        assert upload_server._supervise_psmux_priority in started
