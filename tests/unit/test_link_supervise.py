"""The link thread's supervisor, serve's wiring of it, and the one config
key the link owns (settings.link.remoteControl)."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import threading
import time

import pytest

from magent import config_io, env, link, lockfile, upload_server
from magent.config import ConfigError, Settings, load_config, settings_to_dict
from magent.link import LinkRunner, RelayClient
from tests._fake_relay import FakeRelay
from tests.unit._link_fakes import (
    FakeHost,
    Running,
    new_device,
    save_linked,
    stop_all,
    stray_forwarders,
    write_link,
)


@pytest.fixture(autouse=True)
def _no_runner_outlives_its_test():
    yield
    assert stop_all() == [], "a link runner did not stop"
    assert stray_forwarders() == [], "a forwarder or upload puller did not stop"


@pytest.fixture
def link_on(monkeypatch):
    """MAGENT_LINK=1 in process only (conftest pins 0 for every tier)."""
    monkeypatch.setenv("MAGENT_LINK", "1")
    monkeypatch.setattr(env, "_cached_env", None)


class _Runner:
    """A LinkRunner stand-in replaying scripted outcomes."""

    def __init__(self, outcomes: list[object], made: list[object]) -> None:
        self._outcomes = outcomes
        made.append(self)

    def run(self, stop: threading.Event) -> str:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)


def _factory(outcomes: list[object], made: list[object]):
    return lambda _host: _Runner(outcomes, made)


class TestSupervise:
    def test_magent_link_0_returns_at_once_and_says_so(self, caplog):
        hosts: list[int] = []
        made: list[object] = []
        with caplog.at_level("INFO", logger="magent.link"):
            link.supervise(
                lambda: hosts.append(1),
                threading.Event(),
                runner_factory=_factory(["stopped"], made),
            )
        assert (hosts, made) == ([], [])
        assert [r.getMessage() for r in caplog.records].count(
            "link: off (MAGENT_LINK=0); this serve will not dial the relay"
        ) == 1

    def test_a_stopped_runner_ends_the_thread(self, link_on):
        made: list[object] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(["stopped"], made),
        )
        assert len(made) == 1

    def test_a_crash_is_logged_and_the_runner_restarted_with_backoff(
        self, link_on, caplog
    ):
        made: list[object] = []
        waits: list[float] = []

        def sleep(seconds: float) -> bool:
            waits.append(seconds)
            return False

        with caplog.at_level("ERROR", logger="magent.link"):
            link.supervise(
                object,
                threading.Event(),
                runner_factory=_factory(
                    [RuntimeError("boom"), RuntimeError("boom"), "stopped"], made
                ),
                sleep=sleep,
            )
        assert len(made) == 3
        assert len(waits) == 2
        assert 0 <= waits[0] <= link.SUPERVISE_BACKOFF_BASE_S
        assert 0 <= waits[1] <= 2 * link.SUPERVISE_BACKOFF_BASE_S
        assert any("crashed" in r.getMessage() for r in caplog.records)

    def test_a_host_that_cannot_be_built_is_a_crash_too(self, link_on):
        made: list[object] = []

        def host():
            if not made:
                made.append("failed")
                raise RuntimeError("DECOY serve not ready")
            return object()

        link.supervise(
            host,
            threading.Event(),
            runner_factory=_factory(["stopped"], made),
            sleep=lambda _s: False,
        )
        assert made[0] == "failed" and len(made) == 2

    @pytest.mark.parametrize("terminal", ["revoked", "too_old"])
    def test_after_a_terminal_state_it_waits_for_a_new_link(
        self, link_on, tmp_path, terminal
    ):
        link_path = tmp_path / "link.json"
        link_path.write_text("{}", encoding="utf-8")
        made: list[object] = []
        sleeps: list[float] = []

        def sleep(seconds: float) -> bool:
            sleeps.append(seconds)
            if len(sleeps) == 3:  # the user pairs again: link.json changes
                link_path.unlink()
            return False

        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory([terminal, "stopped"], made),
            sleep=sleep,
            link_path=link_path,
        )
        assert len(made) == 2
        assert sleeps == [link.IDLE_RECHECK_S] * 3

    def test_a_link_that_changed_during_the_run_restarts_at_once(
        self, link_on, tmp_path
    ):
        # Paired again while the old link was still running: the relay then
        # revoked the old one. The new link.json is already there, so there is
        # nothing to wait for.
        link_path = tmp_path / "link.json"
        link_path.write_text("{}", encoding="utf-8")
        made: list[object] = []
        outcomes: list[object] = ["revoked", "stopped"]

        class _Repaired(_Runner):
            def run(self, stop: threading.Event) -> str:
                if len(made) == 1:
                    link_path.write_text('{"again": 1}', encoding="utf-8")
                return super().run(stop)

        sleeps: list[float] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=lambda _h: _Repaired(outcomes, made),
            sleep=lambda s: sleeps.append(s) or len(sleeps) > 3,  # never forever
            link_path=link_path,
        )
        assert len(made) == 2
        assert sleeps == []

    def test_a_relinked_runner_restarts_at_once(self, link_on, tmp_path):
        made: list[object] = []
        sleeps: list[float] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(["relinked", "stopped"], made),
            sleep=lambda s: sleeps.append(s) or len(sleeps) > 3,  # never forever
            link_path=tmp_path / "link.json",
        )
        assert len(made) == 2
        assert sleeps == []

    def test_stop_during_the_wait_ends_the_thread(self, link_on, tmp_path):
        made: list[object] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(["revoked"], made),
            sleep=lambda _s: True,
            link_path=tmp_path / "link.json",
        )
        assert len(made) == 1

    def test_stop_during_the_crash_backoff_ends_the_thread(self, link_on):
        made: list[object] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory([RuntimeError("boom")], made),
            sleep=lambda _s: True,
        )
        assert len(made) == 1

    def test_a_run_that_ended_well_resets_the_crash_backoff(self, link_on, monkeypatch):
        # Full jitter at its ceiling, so every wait is the backoff's ceiling.
        monkeypatch.setattr(
            link, "Backoff", functools.partial(link.Backoff, rand=lambda: 1.0)
        )
        made: list[object] = []
        waits: list[float] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(
                [
                    RuntimeError("boom"),
                    RuntimeError("boom"),
                    "relinked",
                    RuntimeError("boom"),
                    "stopped",
                ],
                made,
            ),
            sleep=lambda s: waits.append(s) or len(waits) > 5,
        )
        base = link.SUPERVISE_BACKOFF_BASE_S
        assert waits == [base, 2 * base, base]

    def test_a_crash_after_a_long_healthy_run_starts_the_backoff_over(
        self, link_on, monkeypatch
    ):
        monkeypatch.setattr(
            link, "Backoff", functools.partial(link.Backoff, rand=lambda: 1.0)
        )
        now = [1_000.0]

        class _Timed(_Runner):
            def run(self, stop: threading.Event) -> str:
                if len(made) == 3:  # the third run lasts a day, then crashes
                    now[0] += 86_400.0
                return super().run(stop)

        made: list[object] = []
        outcomes: list[object] = [
            RuntimeError("a"),
            RuntimeError("b"),
            RuntimeError("c"),
            "stopped",
        ]
        waits: list[float] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=lambda _h: _Timed(outcomes, made),
            sleep=lambda s: waits.append(s) or len(waits) > 5,
            clock=lambda: now[0],
        )
        base = link.SUPERVISE_BACKOFF_BASE_S
        assert waits == [base, 2 * base, base]

    def test_a_repair_with_the_same_size_and_mtime_is_seen(self, link_on, tmp_path):
        # A re-pair replaces link.json atomically: a new file index, even when
        # its size and mtime match the old file's.
        link_path = tmp_path / "link.json"
        link_path.write_text('{"a": 1}', encoding="utf-8")
        before = link_path.stat()
        made: list[object] = []
        sleeps: list[float] = []

        def sleep(seconds: float) -> bool:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                fresh = tmp_path / "fresh.json"
                fresh.write_text('{"b": 2}', encoding="utf-8")
                os.utime(fresh, ns=(before.st_atime_ns, before.st_mtime_ns))
                os.replace(fresh, link_path)
                again = link_path.stat()
                assert (again.st_size, again.st_mtime_ns) == (
                    before.st_size,
                    before.st_mtime_ns,
                )
            return len(sleeps) > 3  # never forever

        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(["revoked", "stopped"], made),
            sleep=sleep,
            link_path=link_path,
        )
        assert len(made) == 2
        assert sleeps == [link.IDLE_RECHECK_S]


class TestOneLinkPerMachine:
    """Every serve starts a link thread, and serve is per port: only one of
    them may run the link. Two runners on one host token would each run a
    command (typed twice into a session) and paste an upload twice."""

    def test_a_second_serve_stands_by_while_another_holds_the_link(
        self, link_on, caplog
    ):
        made: list[object] = []
        with (
            caplog.at_level("INFO", logger="magent.link"),
            lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0),
        ):
            link.supervise(
                object,
                threading.Event(),
                runner_factory=_factory(["stopped"], made),
                sleep=lambda _s: True,
            )
        assert made == []
        assert any("stands by" in r.getMessage() for r in caplog.records)

    def test_once_the_link_is_free_the_standing_serve_runs_it(self, link_on, caplog):
        made: list[object] = []
        held = contextlib.ExitStack()
        held.enter_context(lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0))
        waits: list[float] = []

        def sleep(seconds: float) -> bool:
            waits.append(seconds)
            if len(waits) == 2:
                held.close()  # the other serve exits
            return len(waits) > 5  # never forever

        try:
            with caplog.at_level("INFO", logger="magent.link"):
                link.supervise(
                    object,
                    threading.Event(),
                    runner_factory=_factory(["stopped"], made),
                    sleep=sleep,
                )
        finally:
            held.close()
        assert len(made) == 1
        assert waits == [link.IDLE_RECHECK_S] * 2
        stands = [r for r in caplog.records if "stands by" in r.getMessage()]
        assert len(stands) == 1  # said once, not on every recheck

    def test_two_supervisors_in_one_process_run_one_link(self, link_on):
        stop = threading.Event()
        running = threading.Event()
        runs: list[int] = []

        class _Holding(_Runner):
            def run(self, stop: threading.Event) -> str:
                runs.append(1)
                running.set()
                stop.wait(10)
                return "stopped"

        def one() -> None:
            link.supervise(
                object,
                stop,
                runner_factory=lambda _h: _Holding([], []),
                sleep=lambda s: stop.wait(min(s, 0.02)),
            )

        threads = [threading.Thread(target=one, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        try:
            assert running.wait(5)
            time.sleep(0.2)  # the other thread had its chance to take it
            assert runs == [1]
        finally:
            stop.set()
            for t in threads:
                t.join(5)
        assert not any(t.is_alive() for t in threads)

    def test_the_lock_is_let_go_when_the_thread_ends(self, link_on):
        link.supervise(
            object,
            threading.Event(),
            runner_factory=_factory(["stopped"], []),
        )
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            pass  # free again

    def test_a_lock_that_cannot_be_opened_stands_by_and_never_crashes(
        self, link_on, monkeypatch, caplog
    ):
        def broken(*_a, **_k):
            raise PermissionError("DECOY ~/.magent is read-only")

        monkeypatch.setattr(link.lockfile, "persistent_lock", broken)
        made: list[object] = []
        with caplog.at_level("WARNING", logger="magent.link"):
            link.supervise(
                object,
                threading.Event(),
                runner_factory=_factory(["stopped"], made),
                sleep=lambda _s: True,
            )
        assert made == []
        assert any("link lock" in r.getMessage() for r in caplog.records)


class TestNoDialItWasNotGiven:
    """MAGENT_LINK fails open on a broken MAGENT_* environment, so the thread
    runs; it still dials only the relay link.json names, never a default."""

    @pytest.fixture
    def broken_env(self, monkeypatch, link_on):
        monkeypatch.setenv("MAGENT_NOT_A_THING", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        assert link.link_enabled() is True

    def _dials(self, tmp_path, linked_path):
        stop = threading.Event()
        dialled: list[str] = []

        def client_factory(url, token, *, sleep):
            dialled.append(url)
            stop.set()
            raise ValueError("DECOY: recorded, never dialled")

        def runner(host):
            return LinkRunner(
                host,
                client_factory=client_factory,
                sleep=lambda s: stop.wait(min(s, 0.02)),
                link_path=linked_path,
                state_path=tmp_path / "link-state.json",
            )

        timer = threading.Timer(1.0, stop.set)
        timer.start()
        try:
            link.supervise(FakeHost, stop, runner_factory=runner, link_path=linked_path)
        finally:
            timer.cancel()
        return dialled

    def test_unlinked_it_dials_nothing(self, broken_env, tmp_path):
        assert self._dials(tmp_path, tmp_path / "link.json") == []

    def test_linked_it_dials_only_the_saved_relay(self, broken_env, tmp_path):
        linked = write_link(
            tmp_path,
            "http://127.0.0.1:9",
            (new_device(),),
            host_token="mlh_DECOY_saved",
        )
        assert self._dials(tmp_path, linked.link_path) == ["http://127.0.0.1:9"]


@pytest.fixture
def relay():
    with FakeRelay() as fake:
        yield fake


class TestRelink:
    """A link.json that names another host or token mid-run (`magent link`
    again) ends the run, so the thread restarts on the new credentials
    instead of sealing for new keys under the old token."""

    def test_a_new_token_ends_the_run_as_relinked(self, tmp_path, relay):
        phone = new_device()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.open_streams == 1)  # online, stream held
        relay.host_token = "mlh_DECOY_paired_again"
        save_linked(linked, host_token=relay.host_token)
        relay.set_devices([phone.wire()])  # any devices frame re-reads link.json
        assert run.wait_done() == "relinked"

    def test_a_new_token_found_at_a_reconnect_ends_the_run(self, tmp_path, relay):
        # No devices frame: the reconnect itself must read link.json, before
        # it dials with the old token (which the relay now refuses: a run that
        # dialled first would end "revoked", not "relinked").
        phone = new_device()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.open_streams == 1)  # online, stream held
        save_linked(linked, host_token="mlh_DECOY_paired_again")
        relay.host_token = "mlh_DECOY_paired_again"
        before = len(relay.snapshots)
        relay.drop_streams()
        assert run.wait_done() == "relinked"
        assert len(relay.snapshots) == before  # nothing sent under the old token

    def test_supervise_carries_the_new_link_back_online(self, tmp_path, relay, link_on):
        phone = new_device()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        stop = threading.Event()
        outcomes: list[str] = []

        class _Recorded(LinkRunner):
            def run(self, stop):
                outcomes.append(super().run(stop))
                return outcomes[-1]

        thread = threading.Thread(
            target=link.supervise,
            args=(FakeHost, stop),
            kwargs={
                "runner_factory": lambda h: _Recorded(
                    h,
                    sleep=lambda s: stop.wait(min(s, 0.02)),
                    link_path=linked.link_path,
                    state_path=linked.state_path,
                ),
                "sleep": lambda s: stop.wait(min(s, 0.02)),
                "link_path": linked.link_path,
            },
            daemon=True,
        )
        thread.start()
        try:
            relay.wait_for(lambda: relay.open_streams == 1)
            relay.host_token = "mlh_DECOY_paired_again"
            save_linked(linked, host_token=relay.host_token)
            relay.set_devices([phone.wire()])
            relay.wait_for(lambda: len(relay.snapshots) == 2)
            assert outcomes == ["relinked"]
        finally:
            stop.set()
            while thread.is_alive():
                relay.drop_streams()
                thread.join(0.05)

    def test_after_a_revoke_it_waits_then_runs_the_new_pairing(
        self, tmp_path, relay, link_on
    ):
        phone = new_device()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        stop = threading.Event()

        def fast(seconds: float) -> bool:
            return stop.wait(min(seconds, 0.02))

        thread = threading.Thread(
            target=link.supervise,
            args=(FakeHost, stop),
            kwargs={
                "runner_factory": lambda h: LinkRunner(
                    h,
                    sleep=fast,
                    link_path=linked.link_path,
                    state_path=linked.state_path,
                ),
                "sleep": fast,
                "link_path": linked.link_path,
            },
            daemon=True,
        )
        thread.start()
        try:
            relay.wait_for(lambda: len(relay.snapshots) == 1)
            # `magent unlink` on another machine, or the account: the relay
            # revokes this host; the open stream hears it.
            RelayClient(relay.url, token=relay.host_token).call("DELETE", "/v1/host")
            relay.wait_for(lambda: linked.state().state == "revoked")
            stop.wait(0.3)
            assert len(relay.snapshots) == 1  # no hammering a relay that said no
            with relay._cond:
                relay._revoked = False  # paired again...
            relay.host_token = "mlh_DECOY_paired_again"
            save_linked(linked, host_token=relay.host_token)  # ...link.json changes
            relay.wait_for(lambda: len(relay.snapshots) == 2)
        finally:
            stop.set()
            while thread.is_alive():
                relay.drop_streams()
                thread.join(0.05)


class TestServeOwnsIt:
    def test_run_server_starts_the_link_thread_and_stops_it(
        self, monkeypatch, tmp_path
    ):
        started: list[tuple[object, tuple[object, ...], str | None]] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded = (target, kwargs.get("args", ()), kwargs.get("name"))

            def start(self) -> None:
                started.append(self.recorded)

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(upload_server.threading, "Thread", _Recording)
        monkeypatch.setattr(upload_server, "_bind_addresses", lambda _h: ["127.0.0.1"])
        monkeypatch.setattr(upload_server, "_NoFqdnHTTPServer", _FakeServer)
        monkeypatch.setattr(
            upload_server, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )

        with pytest.raises(KeyboardInterrupt):
            upload_server.run_server(port=0, config_path="C:/cfg/magent.config.json")

        [(path, stop)] = [
            args
            for target, args, _ in started
            if target is upload_server._supervise_link
        ]
        assert [
            name
            for target, _, name in started
            if target is upload_server._supervise_link
        ] == [link.LINK_THREAD]
        assert path == "C:/cfg/magent.config.json"
        assert isinstance(stop, threading.Event)
        assert stop.is_set()

    def test_the_thread_body_is_inert_under_the_test_law(self):
        # conftest pins MAGENT_LINK=0: the real body must return at once.
        done = threading.Event()
        threading.Thread(
            target=lambda: (
                upload_server._supervise_link(None, threading.Event()),
                done.set(),
            ),
            daemon=True,
        ).start()
        assert done.wait(5)

    def test_the_thread_body_runs_the_link_over_this_servers_api(
        self, monkeypatch, link_on
    ):
        hosts: list[object] = []

        def supervise(host_factory, stop):
            hosts.append(host_factory())

        monkeypatch.setattr(link, "supervise", supervise)
        upload_server._supervise_link("C:/cfg/magent.config.json", threading.Event())
        [host] = hosts
        assert host._context == upload_server.UploadHandler._api_context
        assert host._config_path == "C:/cfg/magent.config.json"


class TestConfig:
    def test_remote_control_defaults_off(self):
        assert Settings().link.remote_control is False
        assert settings_to_dict(Settings())["link"] == {"remoteControl": False}

    def test_it_is_read_from_settings_link(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {
                    "version": 4,
                    "projects": [],
                    "settings": {"link": {"remoteControl": True}},
                }
            ),
            encoding="utf-8",
        )
        assert load_config(str(path)).settings.link.remote_control is True

    @pytest.mark.parametrize(
        "bad",
        [
            {"link": "on"},
            {"link": True},
            {"link": {"remoteControl": "true"}},
            {"link": {"remoteControl": 1}},
            {"link": {"remoteControl": None}},
        ],
    )
    def test_a_wrong_type_is_an_error_never_on(self, tmp_path, bad):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"version": 4, "projects": [], "settings": bad}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=r"settings\.link"):
            load_config(str(path))

    def test_an_unknown_link_key_warns(self, tmp_path, capsys):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {
                    "version": 4,
                    "projects": [],
                    "settings": {"link": {"remoteControll": True}},
                }
            ),
            encoding="utf-8",
        )
        assert load_config(str(path)).settings.link.remote_control is False
        assert "settings.link" in capsys.readouterr().err

    def test_set_remote_control_writes_through_config_io(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {"version": 4, "projects": [], "settings": {"psmux": True, "custom": 1}}
            ),
            encoding="utf-8",
        )
        link.set_remote_control(path, True)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["settings"]["link"] == {"remoteControl": True}
        assert data["settings"]["psmux"] is True  # every other key survives
        assert data["settings"]["custom"] == 1
        assert list(config_io.backups_dir().glob("config-*.json"))  # backed up first

        link.set_remote_control(path, False)
        assert load_config(str(path)).settings.link.remote_control is False

    def test_set_remote_control_mends_a_link_block_of_the_wrong_shape(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"version": 4, "projects": [], "settings": {"link": "on"}}),
            encoding="utf-8",
        )
        link.set_remote_control(path, True)
        assert load_config(str(path)).settings.link.remote_control is True
