"""LinkRunner against the in-process fake relay: states, snapshot, commands
(validate, verify, decrypt, run, answer sealed), replays, devices,
revocation, the too-old stop, error classes, backoff and the long-poll
fallback."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from magent import link, linkcrypto, linkfiles
from magent.link import UPGRADE_HINT, RelayClient, RelayError
from magent.linkcrypto import AAD_SNAP
from tests._fake_relay import FakeRelay
from tests.unit._link_fakes import (
    HOST_ID,
    OTHER_ID,
    SESSION_ROW,
    STRANGER_ID,
    TABLET_ID,
    FakeHost,
    Running,
    cmd_id,
    make_command,
    new_device,
    open_result,
    save_linked,
    stop_all,
    stray_forwarders,
    write_link,
)


@pytest.fixture(autouse=True)
def _no_runner_outlives_its_test():
    """Torn down last (autouse), after the relay: a test that failed before
    finish() leaves no runner thread, and no event forwarder, behind."""
    yield
    assert stop_all() == [], "a link runner did not stop"
    assert stray_forwarders() == [], "an event forwarder did not stop"


@pytest.fixture
def relay():
    with FakeRelay() as fake:
        yield fake


@pytest.fixture
def phone():
    return new_device()


def _paths(relay: FakeRelay, prefix: str) -> list[str]:
    return [
        path
        for _method, path, _headers in list(relay.requests)
        if path.startswith(prefix)
    ]


def _queue_raw(relay: FakeRelay, command: dict[str, object]) -> None:
    """Queue a command as a broken relay would: past queue_command's UUID check."""
    with relay._cond:
        relay._commands.append(command)
        relay._cond.notify_all()


def _sealed_reasons(linked, device, relay: FakeRelay) -> list[tuple[int, str]]:
    out = []
    for result in list(relay.results):
        answer = open_result(linked, device, result)
        out.append((answer["status"], answer["body"]["error"]["details"]["reason"]))
    return out


class _Scripted(RelayClient):
    """A real client whose result posts fail as scripted: ``fails[0]`` more
    posts raise ``error``. Also records the timeout of every call."""

    def __init__(self, url, *, token, sleep, fails, error, timeouts):
        super().__init__(url, token=token, sleep=sleep)
        self._fails = fails
        self._error = error
        self._timeouts = timeouts

    def call(self, method, path, body=None, *, token=None, timeout=None, retries=None):
        self._timeouts.append((method, path.partition("?")[0], timeout))
        if method == "POST" and path == "/v1/host/results" and self._fails[0] > 0:
            self._fails[0] -= 1
            raise self._error
        return super().call(
            method, path, body, token=token, timeout=timeout, retries=retries
        )


def _scripted(
    fails: list[int],
    error: RelayError | None = None,
    timeouts: list[tuple[str, str, float | None]] | None = None,
):
    def factory(url, token, *, sleep):
        return _Scripted(
            url,
            token=token,
            sleep=sleep,
            fails=fails,
            error=error or RelayError(0, "network", "scripted drop"),
            timeouts=[] if timeouts is None else timeouts,
        )

    return factory


class TestIdle:
    def test_it_waits_for_link_json_then_connects(self, tmp_path, relay, phone):
        linked = write_link(
            tmp_path, relay.url, (phone,), host_token=relay.host_token, write=False
        )
        run = Running(FakeHost(), linked)
        time.sleep(0.2)
        assert relay.requests == []
        assert run.thread.is_alive()

        save_linked(linked, host_token=relay.host_token)

        relay.wait_for(lambda: relay.snapshots)
        assert run.finish(relay) == "stopped"

    def test_a_broken_link_json_is_reported_once_and_never_dialled(
        self, tmp_path, relay, phone, caplog
    ):
        linked = write_link(
            tmp_path, relay.url, (phone,), host_token=relay.host_token, write=False
        )
        linked.link_path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING", logger="magent.link"):
            run = Running(FakeHost(), linked)
            relay.wait_for(lambda: len(run.sleeps) >= 3)
            assert run.finish(relay) == "stopped"
        assert relay.requests == []
        assert sum("magent unlink" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.parametrize("bad", ["token", "port"])
    def test_a_hand_edited_link_json_is_reported_once_without_a_traceback(
        self, tmp_path, relay, phone, caplog, bad
    ):
        # Pairing never writes either; a hand edit can. The client would
        # refuse them on every call, so they are a bad file, not a host fault.
        url = "http://127.0.0.1:99999" if bad == "port" else relay.url
        token = "bad token DECOY" if bad == "token" else relay.host_token
        linked = write_link(tmp_path, url, (phone,), host_token=token)
        with caplog.at_level("INFO", logger="magent.link"):
            run = Running(FakeHost(), linked)
            relay.wait_for(lambda: len(run.sleeps) >= 3)
            assert run.finish(relay) == "stopped"
        assert relay.requests == []
        assert sum("repair with" in r.getMessage() for r in caplog.records) == 1
        assert [r for r in caplog.records if r.exc_info] == []


class TestSnapshot:
    def test_every_connect_puts_a_sealed_snapshot(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost(sessions=[SESSION_ROW], rc=True, max_bytes=5_000_000)
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)

        snap = relay.snapshots[0]
        assert snap["daemon"] == {
            "version": "9.9.9",
            "platform": "windows",
            "epoch": "e0e0e0e0",
        }
        assert snap["remoteControl"] is True
        assert snap["uploadMaxBytes"] == 5_000_000
        # Routing-only metadata: no path, no model, nothing from the pane.
        assert snap["sessions"] == [
            {
                "session": "magent-alpha",
                "name": "alpha",
                "live": True,
                "hookState": "working",
                "paneState": "busy",
                "nodeState": None,
            }
        ]
        assert set(snap["sealed"]) == {phone.device_id}
        plain = linkcrypto.open_sealed(
            linked.device_keys(phone).h2d,
            snap["sealed"][phone.device_id],
            AAD_SNAP + HOST_ID,
        )
        assert json.loads(plain) == {"sessions": [SESSION_ROW]}

        relay.wait_for(
            lambda: (relay.drop_streams() or True) and len(relay.snapshots) >= 2
        )
        run.finish(relay)

    def test_rows_the_relay_would_refuse_stay_sealed_only(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        odd = [
            {**SESSION_ROW, "session": "..", "name": "dots"},
            {**SESSION_ROW, "session": "has space"},
            {**SESSION_ROW, "session": "long-name", "name": "n" * 300},
            {**SESSION_ROW, "session": "long-state", "hook_state": "x" * 33},
        ]
        host = FakeHost(
            sessions=odd,
            daemon={"version": "v" * 40, "platform": "", "epoch": "e1"},
        )
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)  # the relay accepted it

        snap = relay.snapshots[0]
        assert [s["session"] for s in snap["sessions"]] == ["long-name", "long-state"]
        assert snap["sessions"][0]["name"] == "n" * 200
        assert snap["sessions"][1]["hookState"] is None
        assert snap["daemon"] == {
            "version": "v" * 32,
            "platform": "unknown",
            "epoch": "e1",
        }
        plain = linkcrypto.open_sealed(
            linked.device_keys(phone).h2d,
            snap["sealed"][phone.device_id],
            AAD_SNAP + HOST_ID,
        )
        assert json.loads(plain) == {"sessions": odd}
        run.finish(relay)

    def test_the_state_file_says_online(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: linked.state().state == "online")
        assert linked.state().last_connected is not None
        run.finish(relay)

    def test_a_rate_limited_snapshot_waits_through_the_runner_s_sleep(
        self, tmp_path, relay, phone
    ):
        # The client sleeps through the runner's stop-aware sleep, so a
        # Retry-After never outlives a stop.
        relay.rate_limit_next_calls(1, retry_after=30)
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.snapshots)
        assert 30.0 in run.sleeps
        run.finish(relay)


class TestCommands:
    def test_a_command_runs_once_and_its_result_decrypts(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        run = Running(host, linked)
        path = "/api/v1/sessions/magent-alpha/send"
        relay.queue_command(
            make_command(
                linked,
                phone,
                cid=cmd_id(1),
                seq=5,
                method="POST",
                path=path,
                body={"text": "hi"},
            )
        )

        relay.wait_for(lambda: relay.results)
        assert host.calls == [("POST", path, b'{"text": "hi"}', phone.device_id)]
        result = relay.results[0]
        assert (result["type"], result["commandId"], result["status"]) == (
            "command",
            cmd_id(1),
            200,
        )
        assert open_result(linked, phone, result) == {
            "status": 200,
            "body": {"ok": True, "data": {"path": path}},
        }
        assert 5 in relay.acks
        relay.wait_for(lambda: linked.state().cursor == 5)
        assert cmd_id(1) in linked.state().seen_ids
        run.finish(relay)

    @pytest.mark.parametrize(
        ("forge", "status", "code", "reason"),
        [
            ("stale", 410, "expired", "stale"),
            ("future", 410, "expired", "stale"),
            ("signer", 403, "forbidden", "bad_signature"),
            ("seal", 403, "forbidden", "bad_seal"),
            ("inner", 403, "forbidden", "mismatch"),
        ],
    )
    def test_a_bad_command_is_answered_sealed_and_never_runs(
        self, tmp_path, relay, phone, forge, status, code, reason
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        now = int(time.time() * 1000)
        forged = {
            "stale": {"ts": now - 121_000},
            "future": {"ts": now + 121_000},
            "signer": {"signer": new_device(OTHER_ID)},
            "seal": {"seal_key": os.urandom(32)},
            "inner": {
                "inner": {"method": "GET", "path": "/api/v1/projects", "body": None}
            },
        }[forge]
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(2), seq=2, **forged))

        relay.wait_for(lambda: relay.results)
        assert host.calls == []
        answer = open_result(linked, phone, relay.results[0])
        assert answer["status"] == status
        assert answer["body"]["error"]["code"] == code
        assert answer["body"]["error"]["details"] == {"reason": reason}
        run.finish(relay)

    @pytest.mark.parametrize(
        "fields",
        [
            {"method": "GET\n"},
            {"method": "TRACE"},
            {"method": "get"},
            {"path": "/api/v1/sessions\n/api/v1/projects"},
            {"path": "/api/v1/sessions\x7f"},
            {"path": "/api/v1/sessions\x85"},
            {"path": "/upload"},
            {"path": "/api/v10/sessions"},
            {"ts": f"{int(time.time() * 1000)}\n"},
            {"ts": True},
            {"ts": float(int(time.time() * 1000))},
        ],
        ids=lambda f: repr(f)[:40],
    )
    def test_a_malformed_field_is_answered_400_and_never_runs(
        self, tmp_path, relay, phone, fields
    ):
        # Each command is signed over its own fields, so only the field
        # checks stand between it and LinkHost.handle.
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        inner = {
            "method": fields.get("method", "GET"),
            "path": fields.get("path", "/api/v1/sessions"),
            "body": None,
        }
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_command(
            make_command(linked, phone, cid=cmd_id(3), seq=1, inner=inner, **fields)
        )

        relay.wait_for(lambda: relay.results)
        assert host.calls == []
        answer = open_result(linked, phone, relay.results[0])
        assert answer["status"] == 400
        assert answer["body"]["error"]["code"] == "invalid_request"
        assert answer["body"]["error"]["details"] == {"reason": "malformed"}
        run.finish(relay)

    @pytest.mark.parametrize(
        "cid",
        [
            cmd_id(4) + "\n",
            "\n" + cmd_id(4),
            cmd_id(4).replace("-", "\n", 1),
            "not-a-uuid",
            cmd_id(4).replace("-", ""),
            "{" + cmd_id(4) + "}",
        ],
    )
    def test_a_malformed_id_is_only_acknowledged(self, tmp_path, relay, phone, cid):
        # No answer can carry it (the relay takes UUID command ids only), and
        # it never runs, though its signature is good.
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        run = Running(host, linked)
        _queue_raw(relay, make_command(linked, phone, cid=cid, seq=4))

        relay.wait_for(lambda: 4 in relay.acks)
        assert host.calls == []
        assert relay.results == []
        relay.wait_for(lambda: linked.state().cursor == 4)
        run.finish(relay)

    def test_a_command_from_an_untrusted_device_is_only_acknowledged(
        self, tmp_path, relay, phone
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        stranger = new_device(STRANGER_ID)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_command(make_command(linked, stranger, cid=cmd_id(5), seq=3))

        relay.wait_for(lambda: 3 in relay.acks)
        assert host.calls == []
        assert relay.results == []
        run.finish(relay)

    def test_the_id_is_on_disk_before_the_command_runs(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        on_disk: list[bool] = []
        host = FakeHost(
            on_handle=lambda m, p: on_disk.append(cmd_id(6) in linked.state().seen_ids)
        )
        run = Running(host, linked)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(6), seq=1))

        relay.wait_for(lambda: relay.results)
        assert on_disk == [True]
        run.finish(relay)

    def test_an_id_that_cannot_be_saved_never_runs(
        self, monkeypatch, tmp_path, relay, phone
    ):
        # Without the id on disk a restart could run it again, so it is
        # answered 503 instead (the device may send it again as a new one).
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)

        def full_disk(state, path=None):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(link.linkfiles, "save_state", full_disk)
        monkeypatch.setattr(link, "SAVE_RETRY_S", 0.0)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(7), seq=1))

        relay.wait_for(lambda: relay.results)
        assert host.calls == []
        assert _sealed_reasons(linked, phone, relay) == [(503, "state_unsaved")]
        run.finish(relay)

    def test_a_crashing_handler_is_answered_500(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)

        def boom(method, path):
            raise RuntimeError("DECOY handler crash")

        run = Running(FakeHost(on_handle=boom), linked)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(8), seq=1))

        relay.wait_for(lambda: relay.results)
        assert _sealed_reasons(linked, phone, relay) == [(500, "crashed")]
        assert run.thread.is_alive()
        run.finish(relay)

    def test_a_lost_result_is_resent_from_the_cache_not_run_again(
        self, tmp_path, relay, phone
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        fails = [1]
        host = FakeHost()
        run = Running(host, linked, client_factory=_scripted(fails))
        relay.queue_command(make_command(linked, phone, cid=cmd_id(9), seq=4))

        relay.wait_for(lambda: relay.results)
        assert fails == [0]
        assert len(host.calls) == 1
        assert relay.results[0]["commandId"] == cmd_id(9)
        # Replayed on a reconnect from the unmoved cursor.
        assert len(_paths(relay, "/v1/stream/host?since=0")) >= 2
        run.finish(relay)

    def test_after_a_restart_a_replayed_command_is_only_acknowledged(
        self, tmp_path, relay, phone
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        first = Running(host, linked, client_factory=_scripted([10**6]))
        relay.queue_command(make_command(linked, phone, cid=cmd_id(10), seq=6))
        relay.wait_for(lambda: host.calls and cmd_id(10) in linked.state().seen_ids)
        first.finish(relay)
        assert relay.results == []
        assert linked.state().cursor == 0  # no ack, so the cursor never moved

        second = Running(host, linked)
        relay.wait_for(lambda: 6 in relay.acks)
        assert len(host.calls) == 1  # remembered across the restart: never run twice
        assert relay.results == []
        second.finish(relay)

    def test_a_refused_result_is_acknowledged_so_the_stream_moves_on(
        self, tmp_path, relay, phone, caplog
    ):
        # A permanent refusal would be replayed forever; the seq is acked
        # alone and the next command still runs.
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        error = RelayError(413, "payload_too_large", "DECOY too big")
        with caplog.at_level("WARNING", logger="magent.link"):
            run = Running(host, linked, client_factory=_scripted([1], error))
            relay.queue_command(make_command(linked, phone, cid=cmd_id(11), seq=1))
            relay.wait_for(lambda: 1 in relay.acks)
            assert relay.results == []

            relay.queue_command(make_command(linked, phone, cid=cmd_id(12), seq=2))
            relay.wait_for(lambda: relay.results)
            run.finish(relay)
        assert [r["commandId"] for r in relay.results] == [cmd_id(12)]
        assert len(host.calls) == 2
        assert any("413" in r.getMessage() for r in caplog.records)

    def test_refusals_never_flush_a_real_id_out_of_the_seen_ring(
        self, monkeypatch, tmp_path, relay, phone
    ):
        # Only a verified command takes a place in the ring. A relay that
        # floods refusals (forged or untrusted) and then replays a real signed
        # command under a new seq gets the cached answer, never a second run.
        # Refusals stay out of the result cache too, or they would evict the
        # real answer and the replay would only be acknowledged.
        monkeypatch.setattr(link, "SEEN_MAX", 4)
        monkeypatch.setattr(link, "RESULT_CACHE_MAX", 4)
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        run = Running(host, linked)
        real = make_command(linked, phone, cid=cmd_id(20), seq=1)
        relay.queue_command(real)
        relay.wait_for(lambda: relay.results)
        stranger, forger = new_device(STRANGER_ID), new_device(OTHER_ID)
        for n in range(5):
            relay.queue_command(
                make_command(linked, stranger, cid=cmd_id(21 + n), seq=2 + n)
            )
            relay.queue_command(
                make_command(
                    linked, phone, cid=cmd_id(31 + n), seq=7 + n, signer=forger
                )
            )
        relay.wait_for(lambda: 11 in relay.acks)

        with relay._cond:
            relay.results[:] = [
                r for r in relay.results if r["commandId"] != cmd_id(20)
            ]
        _queue_raw(relay, {**real, "seq": 12})
        relay.drop_streams()

        relay.wait_for(lambda: 12 in relay.acks)
        assert len(host.calls) == 1
        assert [r["commandId"] for r in relay.results if r["commandId"] == cmd_id(20)]
        assert linked.state().seen_ids == (cmd_id(20),)
        run.finish(relay)

    def test_a_reconnect_resumes_from_the_cursor_with_a_fresh_snapshot(
        self, tmp_path, relay, phone
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(13), seq=9))
        relay.wait_for(lambda: linked.state().cursor == 9)

        relay.wait_for(
            lambda: (
                (relay.drop_streams() or True)
                and _paths(relay, "/v1/stream/host?since=9")
            )
        )
        assert len(relay.snapshots) >= 2
        run.finish(relay)


class TestDevices:
    def test_a_revoked_device_is_forgotten_and_refused(self, tmp_path, relay, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        linked = write_link(
            tmp_path, relay.url, (phone, tablet), host_token=relay.host_token
        )
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)

        relay.set_devices(
            [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
        )

        relay.wait_for(lambda: linked.device_ids() == [phone.device_id])
        relay.wait_for(lambda: set(relay.snapshots[-1]["sealed"]) == {phone.device_id})
        relay.queue_command(make_command(linked, tablet, cid=cmd_id(14), seq=7))
        relay.wait_for(lambda: 7 in relay.acks)
        assert host.calls == []
        run.finish(relay)

    def test_trust_is_never_added_from_the_relay(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        stranger = new_device(STRANGER_ID)
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)
        relay.set_devices([phone.wire(), stranger.wire()])

        relay.queue_command(make_command(linked, stranger, cid=cmd_id(15), seq=1))
        relay.wait_for(lambda: 1 in relay.acks)
        assert host.calls == []
        assert relay.results == []
        assert linked.device_ids() == [phone.device_id]
        assert all(set(s["sealed"]) == {phone.device_id} for s in relay.snapshots)
        run.finish(relay)

    def test_a_device_paired_while_running_is_picked_up(self, tmp_path, relay, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)

        # `magent link` (add device) saved it; the relay announces the pairing.
        save_linked(
            linked,
            host_token=relay.host_token,
            devices=(phone.trusted(), tablet.trusted()),
        )
        relay.set_devices([phone.wire(), tablet.wire()])

        relay.wait_for(
            lambda: set(relay.snapshots[-1]["sealed"]) == {phone.device_id, TABLET_ID}
        )
        relay.queue_command(make_command(linked, tablet, cid=cmd_id(16), seq=1))
        relay.wait_for(lambda: relay.results)
        assert [call[3] for call in host.calls] == [TABLET_ID]
        run.finish(relay)

    def test_a_link_json_read_that_fails_once_is_retried(
        self, monkeypatch, tmp_path, relay, phone
    ):
        # On Windows a read during another writer's atomic swap fails for a
        # moment; the revocation must still reach link.json.
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        linked = write_link(
            tmp_path, relay.url, (phone, tablet), host_token=relay.host_token
        )
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.snapshots)
        real_load = link.linkfiles.load_link
        fails = [1]

        def flaky_load(path=None):
            # Only the runner's reads fail: the test's own reads stay out of it.
            if threading.current_thread() is run.thread and fails[0] > 0:
                fails[0] -= 1
                raise linkfiles.LinkFileError("DECOY sharing violation")
            return real_load(path)

        monkeypatch.setattr(link.linkfiles, "load_link", flaky_load)
        relay.set_devices(
            [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
        )
        relay.wait_for(lambda: linked.device_ids() == [phone.device_id])
        assert fails == [0]
        run.finish(relay)

    def test_a_revocation_save_that_fails_once_is_retried(
        self, monkeypatch, tmp_path, relay, phone
    ):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        linked = write_link(
            tmp_path, relay.url, (phone, tablet), host_token=relay.host_token
        )
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.snapshots)
        real_save = link.linkfiles.save_link
        fails = [1]

        def flaky_save(link_file, path=None):
            if threading.current_thread() is run.thread and fails[0] > 0:
                fails[0] -= 1
                raise PermissionError(13, "DECOY sharing violation")
            real_save(link_file, path)

        monkeypatch.setattr(link.linkfiles, "save_link", flaky_save)
        relay.set_devices(
            [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
        )
        relay.wait_for(lambda: linked.device_ids() == [phone.device_id])
        assert fails == [0]
        run.finish(relay)

    def test_a_revocation_applies_even_when_link_json_cannot_be_read(
        self, monkeypatch, tmp_path, relay, phone
    ):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        linked = write_link(
            tmp_path, relay.url, (phone, tablet), host_token=relay.host_token
        )
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(lambda: relay.snapshots)

        def unreadable(path=None):
            raise linkfiles.LinkFileError("DECOY unreadable")

        monkeypatch.setattr(link, "SAVE_RETRY_S", 0.0)
        monkeypatch.setattr(link.linkfiles, "load_link", unreadable)
        relay.set_devices(
            [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
        )

        relay.wait_for(lambda: set(relay.snapshots[-1]["sealed"]) == {phone.device_id})
        relay.queue_command(make_command(linked, tablet, cid=cmd_id(19), seq=1))
        relay.wait_for(lambda: 1 in relay.acks)
        assert host.calls == []
        assert relay.results == []
        run.finish(relay)

    def test_a_device_with_unusable_keys_is_skipped_not_fatal(
        self, tmp_path, relay, phone, caplog
    ):
        # A link.json written before pairing refused weak keys: an all-zero
        # kexPub gives a zero shared secret, which pair_keys refuses.
        bad = new_device(TABLET_ID, "Weak DECOY")
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        weak = bad.trusted()
        weak = linkfiles.TrustedDevice(
            device_id=weak.device_id,
            name=weak.name,
            kex_pub=linkcrypto.b64u_encode(bytes(32)),
            sign_pub=weak.sign_pub,
            paired_at=weak.paired_at,
        )
        save_linked(
            linked, host_token=relay.host_token, devices=(phone.trusted(), weak)
        )
        host = FakeHost()
        with caplog.at_level("WARNING", logger="magent.link"):
            run = Running(host, linked)
            relay.wait_for(lambda: relay.snapshots)
            relay.queue_command(make_command(linked, phone, cid=cmd_id(17), seq=1))
            relay.wait_for(lambda: relay.results)
            run.finish(relay)
        assert set(relay.snapshots[0]["sealed"]) == {phone.device_id}
        assert len(host.calls) == 1
        assert any(TABLET_ID in r.getMessage() for r in caplog.records)


class TestStops:
    def test_a_revoked_frame_stops_the_runner(self, tmp_path, relay, phone):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: relay.snapshots)
        relay.revoke()
        assert run.wait_done() == "revoked"
        assert linked.state().state == "revoked"

    def test_a_401_on_connect_stops_the_runner(self, tmp_path, relay, phone):
        relay.revoke()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        assert run.wait_done() == "revoked"
        assert linked.state().state == "revoked"

    def test_a_426_stops_with_one_upgrade_hint(self, tmp_path, relay, phone, caplog):
        relay.set_protocol_too_old()
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        with caplog.at_level("WARNING", logger="magent.link"):
            run = Running(FakeHost(), linked)
            assert run.wait_done() == "too_old"
        assert [r.getMessage() for r in caplog.records].count(UPGRADE_HINT) == 1
        assert linked.state().state == "too_old"
        assert len(relay.requests) == 1  # stopped at once, no retry loop


def _spy_states(monkeypatch) -> list[tuple[str, float | None]]:
    states: list[tuple[str, float | None]] = []
    real_save = link.linkfiles.save_state

    def spy(state, path=None):
        states.append((state.state, state.retry_at))
        real_save(state, path)

    monkeypatch.setattr(link.linkfiles, "save_state", spy)
    return states


class TestBackoff:
    def test_offline_carries_a_retry_time(self, monkeypatch, tmp_path, relay, phone):
        relay.fail_next_connects(1)
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        states = _spy_states(monkeypatch)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) >= 2)
        run.finish(relay)
        offline = [retry for state, retry in states if state == "offline"]
        assert offline
        assert all(retry is not None for retry in offline)
        # A 503 is transient: the first wait is a jittered second, at most.
        assert run.sleeps[0] <= 1.0

    def test_a_stream_refusal_honours_its_retry_after(
        self, monkeypatch, tmp_path, relay, phone
    ):
        relay.fail_next_connects(1, 429, retry_after=7)
        now = [2_000.0]
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        states = _spy_states(monkeypatch)
        run = Running(FakeHost(), linked, clock=lambda: now[0])
        relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) >= 2)
        run.finish(relay)
        assert run.sleeps[0] == 7.0
        assert ("offline", 2_007.0) in states

    def test_a_permanent_refusal_waits_the_long_retry(self, tmp_path, relay, phone):
        relay.fail_next_connects(1, 404)
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) >= 2)
        run.finish(relay)
        assert run.sleeps[0] == link.PERMANENT_RETRY_S

    def test_a_connection_that_lasted_a_minute_resets_the_backoff(
        self, monkeypatch, tmp_path, relay, phone
    ):
        resets: list[int] = []

        class Recording(link.Backoff):
            def reset(self) -> None:
                resets.append(1)
                super().reset()

        monkeypatch.setattr(link, "Backoff", Recording)
        now = [1_000.0]
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked, clock=lambda: now[0])
        relay.wait_for(lambda: _paths(relay, "/v1/stream/host"))
        relay.drop_streams()
        relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) >= 2)
        assert resets == []
        now[0] += 61

        relay.wait_for(lambda: (relay.drop_streams() or True) and resets)
        run.finish(relay)

    def test_three_failed_connects_switch_to_long_polling(self, tmp_path, phone):
        timeouts: list[tuple[str, str, float | None]] = []
        with FakeRelay(long_poll_s=1.0) as relay:
            relay.fail_next_connects(3)
            linked = write_link(
                tmp_path, relay.url, (phone,), host_token=relay.host_token
            )
            host = FakeHost()
            run = Running(host, linked, client_factory=_scripted([0], None, timeouts))
            relay.wait_for(lambda: _paths(relay, "/v1/host/commands?since=0&wait=25"))
            relay.queue_command(make_command(linked, phone, cid=cmd_id(18), seq=2))

            relay.wait_for(lambda: relay.results)
            assert len(host.calls) == 1
            assert len(_paths(relay, "/v1/stream/host")) == 3
            run.finish(relay)
        polls = [t for m, p, t in timeouts if p == "/v1/host/commands"]
        assert polls
        assert set(polls) == {link.LONG_POLL_TIMEOUT_S}

    def test_long_polling_applies_revocations_too(self, tmp_path, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        with FakeRelay(long_poll_s=0.2) as relay:
            relay.fail_next_connects(3)
            linked = write_link(
                tmp_path, relay.url, (phone, tablet), host_token=relay.host_token
            )
            run = Running(FakeHost(), linked)
            relay.wait_for(lambda: _paths(relay, "/v1/host/commands"))
            relay.set_devices(
                [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
            )
            relay.wait_for(lambda: linked.device_ids() == [phone.device_id])
            run.finish(relay)

    def test_after_thirty_minutes_of_polling_the_stream_is_tried_again(
        self, tmp_path, phone
    ):
        with FakeRelay(long_poll_s=0.2) as relay:
            relay.fail_next_connects(3)
            now = [5_000.0]
            linked = write_link(
                tmp_path, relay.url, (phone,), host_token=relay.host_token
            )
            run = Running(FakeHost(), linked, clock=lambda: now[0])
            relay.wait_for(lambda: _paths(relay, "/v1/host/commands"))
            assert len(_paths(relay, "/v1/stream/host")) == 3

            now[0] += link.LONG_POLL_FOR_S + 1

            relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) == 4)
            run.finish(relay)

    def test_mid_stream_drops_never_switch_to_long_polling(
        self, tmp_path, relay, phone
    ):
        # Each connection delivers frames, then is lost (its result post
        # fails): that is not a failed connect, however often it happens.
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        fails = [3]
        host = FakeHost()
        run = Running(host, linked, client_factory=_scripted(fails))
        relay.queue_command(make_command(linked, phone, cid=cmd_id(40), seq=1))
        relay.wait_for(lambda: relay.results)
        assert fails == [0]
        assert len(_paths(relay, "/v1/stream/host")) >= 4
        assert _paths(relay, "/v1/host/commands") == []
        run.finish(relay)

    @pytest.mark.parametrize("unackable", [False, True], ids=["empty", "unackable"])
    def test_a_poll_that_moves_nothing_waits_out_the_floor(
        self, monkeypatch, tmp_path, phone, unackable
    ):
        # The relay answers at once (long_poll_s=0): a round that came back
        # empty, or with nothing the runner could ack, waits out the rest of
        # POLL_MIN_INTERVAL_S instead of polling again in a tight loop.
        monkeypatch.setattr(link, "POLL_MIN_INTERVAL_S", 1234.0)
        with FakeRelay() as relay:
            relay.fail_next_connects(3)
            linked = write_link(
                tmp_path, relay.url, (phone,), host_token=relay.host_token
            )
            if unackable:
                # The relay returns it on every poll; the runner cannot ack
                # a seq that is not an int, so it never moves the cursor.
                bad = make_command(linked, phone, cid=cmd_id(41), seq=1)
                _queue_raw(relay, {**bad, "seq": "1"})
            run = Running(FakeHost(), linked)
            relay.wait_for(lambda: len(_paths(relay, "/v1/host/commands")) >= 2)
            run.finish(relay)
        floors = [s for s in run.sleeps if 1233.0 < s <= 1234.0]
        assert len(floors) >= 1

    def test_a_poll_that_moved_the_cursor_is_not_floored(
        self, monkeypatch, tmp_path, phone
    ):
        # The other half of the floor: a round that answered a command moved
        # the cursor, so the next poll goes out at once. The round after it
        # comes back empty and is floored, which proves the patch took.
        monkeypatch.setattr(link, "POLL_MIN_INTERVAL_S", 1234.0)
        polls: list[int] = []  # len(run.sleeps) as each poll went out

        class Marking(RelayClient):
            def call(self, method, path, body=None, **kwargs):
                if path.startswith("/v1/host/commands"):
                    polls.append(len(self._sleep.__self__.sleeps))
                return super().call(method, path, body, **kwargs)

        def factory(url, token, *, sleep):
            return Marking(url, token=token, sleep=sleep)

        with FakeRelay() as relay:
            relay.fail_next_connects(3)
            linked = write_link(
                tmp_path, relay.url, (phone,), host_token=relay.host_token
            )
            relay.queue_command(make_command(linked, phone, cid=cmd_id(42), seq=1))
            run = Running(FakeHost(), linked, client_factory=factory)
            relay.wait_for(lambda: relay.results and len(polls) >= 3)
            run.finish(relay)

        def floored(first: int, last: int) -> bool:
            return any(1233.0 < s <= 1234.0 for s in run.sleeps[first:last])

        assert not floored(polls[0], polls[1])  # the productive round
        assert floored(polls[1], polls[2])  # the empty one after it

    def test_a_huge_retry_after_is_capped(self, tmp_path, relay, phone):
        relay.fail_next_connects(1, 429, retry_after=99999)
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: len(_paths(relay, "/v1/stream/host")) >= 2)
        run.finish(relay)
        assert run.sleeps[0] == link.RETRY_AFTER_MAX_S


class TestResilience:
    def test_without_cryptography_the_thread_idles(
        self, monkeypatch, tmp_path, relay, phone, caplog
    ):
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)

        def missing():
            raise linkcrypto.LinkCryptoUnavailable("DECOY no cryptography")

        monkeypatch.setattr(linkcrypto, "_require", missing)
        with caplog.at_level("WARNING", logger="magent.link"):
            run = Running(FakeHost(), linked)
            time.sleep(0.5)
            assert run.thread.is_alive()
            assert run.finish(relay) == "stopped"
        hints = [r for r in caplog.records if linkcrypto.INSTALL_HINT in r.getMessage()]
        assert len(hints) == 1
        assert relay.requests == []

    def test_a_failing_link_host_backs_off_and_retries(self, tmp_path, relay, phone):
        # A LinkHost failure outside handle() (the snapshot here) is retried
        # like a dropped connection; it neither ends the thread nor counts
        # toward the long-poll switch.
        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        fails = [3]

        class Flaky(FakeHost):
            def snapshot(self):
                if fails[0] > 0:
                    fails[0] -= 1
                    raise RuntimeError("DECOY snapshot crash")
                return super().snapshot()

        run = Running(Flaky(), linked)
        relay.wait_for(lambda: relay.snapshots)
        relay.wait_for(lambda: _paths(relay, "/v1/stream/host"))
        assert run.thread.is_alive()
        assert fails == [0]
        assert _paths(relay, "/v1/host/commands") == []
        assert run.finish(relay) == "stopped"
