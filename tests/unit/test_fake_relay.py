"""The fake relay behaves like the relay's host side (C3/C5).

Each fidelity rule here is one the real relay
enforces, so a daemon bug the relay would catch cannot pass against the fake.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import threading
import time

import pytest

from magent.link import RelayClient, RelayError, Revoked, TooOld
from tests._fake_relay import (
    CODE_ALPHABET,
    HOST_STREAM_LIMIT,
    PAIRING_CODE,
    FakeRelay,
    _Handler,
    normalize_code,
)

COMMAND = {
    "id": "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8c",
    "seq": 1,
    "deviceId": "dev_1",
    "method": "GET",
    "path": "/api/v1/meta",
    "sealed": "s" * 40,
    "sig": "g" * 86,
    "ts": 1791500000000,
    "expiresAt": "2026-10-08T22:00:30.000Z",
}
EVENT = {"daemonEventId": "e:1", "type": "session.state", "meta": {}, "sealed": {}}
SNAPSHOT = {
    "daemon": {"version": "4.0.0", "platform": "win32", "epoch": "ep1"},
    "remoteControl": True,
    "uploadMaxBytes": 1_048_576,
    "sessions": [],
    "sealed": {},
}
SESSION = {
    "session": "api",
    "name": "api",
    "live": True,
    "hookState": "working",
    "paneState": None,
    "nodeState": None,
}
UPLOAD_ID = "0190f3a2-7b1c-7d4e-8a5b-0000000000aa"
C2 = "0190f3a2-7b1c-7d4e-8a5b-0000000000c2"
SEALED = "z" * 40
KEX, SIGN = "K" * 43, "S" * 43
PAIR_START = {
    "hostKexPub": KEX,
    "hostSignPub": SIGN,
    "name": "box",
    "platform": "win32",
    "daemonVersion": "4.0.0",
}
DEVICE = {
    "deviceId": "dev_1",
    "name": "phone",
    "kexPub": "k" * 43,
    "signPub": "s" * 43,
    "pairedAt": "2026-10-08T21:00:00.000Z",
    "revokedAt": None,
}


@pytest.fixture
def relay(fake_relay):
    return fake_relay


def _host(relay: FakeRelay, **kwargs) -> RelayClient:
    return RelayClient(relay.url, token=relay.host_token, **{"timeout": 5, **kwargs})


def _command(seq: int, ident: str | None = None) -> dict[str, object]:
    return {**COMMAND, "id": ident or f"0190f3a2-7b1c-7d4e-8a5b-{seq:012d}", "seq": seq}


def _refused(call) -> RelayError:
    with pytest.raises(RelayError) as exc:
        call()
    return exc.value


def _raw(
    relay: FakeRelay,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    token: bool = True,
):
    """One request on a plain http.client connection: (connection, response)."""
    conn = http.client.HTTPConnection(*relay.address, timeout=5)
    headers = {"X-Magent-Link": "1"}
    if token:
        headers["Authorization"] = f"Bearer {relay.host_token}"
    conn.request(method, path, body=body, headers=headers)
    return conn, conn.getresponse()


def _take(client: RelayClient, n: int, path: str = "/v1/stream/host"):
    """The first ``n`` events of a fresh stream, read on a helper thread; the
    stream is closed afterwards so it gives its slot back."""
    out = []
    stream = client.stream(path, read_timeout=5)
    done = threading.Event()

    def read():
        for event in stream:
            out.append(event)
            if len(out) == n:
                break
        stream.close()
        done.set()

    threading.Thread(target=read, daemon=True).start()
    assert done.wait(5)
    return out


# -- the server itself ---------------------------------------------------------


def test_it_listens_on_loopback_where_magent_link_url_points(relay):
    from magent.link import relay_url

    host, port = relay.address
    assert ipaddress.ip_address(host).is_loopback
    assert relay.url == f"http://{host}:{port}"
    assert relay_url() == relay.url


def test_the_serve_thread_is_gone_after_the_block():
    with FakeRelay() as fake:
        stream = _host(fake).stream("/v1/stream/host", read_timeout=5)
        assert next(stream).event == "devices"
        thread = fake.serve_thread
        assert thread is not None and thread.is_alive()
    assert not thread.is_alive()
    assert fake.open_streams == 0
    stream.close()


def test_a_client_that_never_sends_its_body_does_not_hold_teardown():
    # A request that announces a body it never sends leaves its handler in a
    # socket read; the server-side socket timeout frees it, and teardown is
    # bounded either way.
    started = time.monotonic()
    with FakeRelay() as fake:
        conn = http.client.HTTPConnection(*fake.address, timeout=5)
        conn.putrequest("POST", "/v1/host/ack")
        conn.putheader("Content-Length", "100")
        conn.endheaders()
        time.sleep(0.2)
    conn.close()
    assert time.monotonic() - started < 15


def test_teardown_checks_never_mask_a_failure_in_the_block():
    with pytest.raises(ValueError, match="the test's own failure"), FakeRelay():
        raise ValueError("the test's own failure")


def test_a_refusal_before_the_body_is_read_still_reaches_the_client(relay):
    # The body is drained before any reply: on Windows a reply sent over an
    # unread body resets the socket, and the client saw a network error
    # instead of the 4xx. A large body makes that reset near-certain.
    big = {"seq": 1, "pad": "x" * (4 * 1024 * 1024)}
    wrong = RelayClient(relay.url, token="mlh_DECOY_wrong", timeout=10)
    for _ in range(5):
        with pytest.raises(Revoked):
            wrong.call("POST", "/v1/host/ack", big)
    relay.rate_limit_next_calls(5, retry_after=90)
    limited = _host(relay, timeout=10)
    for _ in range(5):
        err = _refused(lambda: limited.call("PUT", "/v1/host/snapshot", big))
        assert (err.status, err.code, err.retry_after) == (429, "rate_limited", 90.0)
    relay.set_protocol_too_old()
    for _ in range(5):
        with pytest.raises(TooOld):
            limited.call("POST", "/v1/host/ack", big)


# -- auth, protocol, routing ---------------------------------------------------


def test_host_calls_need_the_token_and_the_protocol_header(relay):
    with pytest.raises(Revoked):
        RelayClient(relay.url, token="mlh_DECOY_wrong").call("GET", "/v1/host/devices")
    relay.set_protocol_too_old()
    with pytest.raises(TooOld):
        _host(relay).call("GET", "/v1/host/devices")


def test_a_wrong_method_is_405_with_allow(relay):
    conn, resp = _raw(relay, "GET", "/v1/host/ack")
    try:
        body = json.loads(resp.read())
        assert (resp.status, resp.getheader("Allow")) == (405, "POST")
        assert (body["ok"], body["error"]["code"]) == (False, "method_not_allowed")
    finally:
        conn.close()
    assert _refused(lambda: _host(relay).call("GET", "/v1/nope")).status == 404


# -- upstream calls ------------------------------------------------------------


def test_upstream_calls_are_recorded(relay):
    relay.queue_command(_command(3))
    client = _host(relay)
    snapshot = {
        **SNAPSHOT,
        "sessions": [SESSION, {**SESSION, "live": False}],
        "sealed": {"dev_1": SEALED},
    }
    assert client.call("PUT", "/v1/host/snapshot", snapshot) == {"sessions": 1}
    assert client.call("POST", "/v1/host/ack", {"seq": 3}) == {"seq": 3}
    assert client.call("POST", "/v1/host/events", {"events": [EVENT]}) == {
        "accepted": 1,
        "lastSeq": 1,
    }
    assert relay.snapshots == [snapshot]
    assert relay.acks == [3]
    assert relay.events == [EVENT]
    assert ("PUT", "/v1/host/snapshot") in [(m, p) for m, p, _ in relay.requests]


def test_an_oversized_event_batch_is_413(relay):
    body = {"events": [{**EVENT, "meta": {"pad": "x" * (256 * 1024)}}]}
    err = _refused(lambda: _host(relay).call("POST", "/v1/host/events", body))
    assert (err.status, err.code) == (413, "payload_too_large")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/host/events", {"events": []}),
        ("POST", "/v1/host/events", {"events": [EVENT] * 501}),
        ("POST", "/v1/host/events", {"events": [{"daemonEventId": "e:1"}]}),
        (
            "POST",
            "/v1/host/results",
            {
                "results": [
                    {"type": "upload", "uploadId": UPLOAD_ID, "state": "pulled"}
                ]
                * 101
            },
        ),
        (
            "POST",
            "/v1/host/results",
            {"results": [{"type": "command", "commandId": "c1", "status": 200}]},
        ),
        ("POST", "/v1/host/results", {}),
        ("POST", "/v1/host/ack", {}),
        ("POST", "/v1/host/ack", {"seq": "1"}),
        ("PUT", "/v1/host/snapshot", {"sessions": []}),
        *[
            ("PUT", "/v1/host/snapshot", {**SNAPSHOT, "sessions": [row]})
            for row in (
                {**SESSION, "session": ".."},
                {**SESSION, "session": "."},
                {**SESSION, "session": "a/b"},
                {**SESSION, "session": "s" * 129},
                {**SESSION, "name": "n" * 201},
                {**SESSION, "live": "yes"},
                {k: v for k, v in SESSION.items() if k != "hookState"},
                {**SESSION, "paneState": "p" * 33},
                {**SESSION, "nodeState": 1},
                {
                    k if k != "hookState" else "hook_state": v
                    for k, v in SESSION.items()
                },
            )
        ],
        *[
            ("PUT", "/v1/host/snapshot", {**SNAPSHOT, "sealed": {"dev_1": blob}})
            for blob in ("z" * 37, "z" * 1_400_001, "z" * 39 + "=", "z" * 39 + "+", 7)
        ],
        ("POST", "/v1/host/events", {"events": [{**EVENT, "sealed": {"d": "z"}}]}),
        (
            "POST",
            "/v1/host/events",
            {"events": [{**EVENT, "sealed": {"d": "z/" * 20}}]},
        ),
        (
            "POST",
            "/v1/host/results",
            {
                "results": [
                    {
                        "type": "command",
                        "commandId": "c1",
                        "status": 200,
                        "sealed": SEALED,
                    }
                ]
            },
        ),
        (
            "POST",
            "/v1/host/results",
            {
                "results": [
                    {
                        "type": "command",
                        "commandId": "0" * 36,
                        "status": 200,
                        "sealed": SEALED,
                    }
                ]
            },
        ),
        (
            "POST",
            "/v1/host/results",
            {"results": [{"type": "upload", "uploadId": "u1", "state": "pulled"}]},
        ),
        ("GET", "/v1/host/commands?since=abc", None),
        ("GET", "/v1/host/commands?since=5%0A", None),
        ("GET", "/v1/host/commands?since=%EF%BC%95", None),
        ("GET", "/v1/host/commands?wait=-1", None),
    ],
)
def test_bodies_and_queries_the_relay_refuses_are_400(relay, method, path, body):
    err = _refused(lambda: _host(relay).call(method, path, body))
    assert (err.status, err.code) == (400, "invalid_request")
    assert relay.events == relay.results == relay.snapshots == relay.acks == []


def test_an_ack_past_the_last_command_is_refused(relay):
    relay.queue_command(COMMAND)
    client = _host(relay)
    err = _refused(lambda: client.call("POST", "/v1/host/ack", {"seq": 2}))
    assert (err.status, err.code) == (400, "invalid_request")
    assert relay.acks == []
    result = {
        "type": "command",
        "commandId": COMMAND["id"],
        "status": 200,
        "sealed": "x" * 40,
    }
    err = _refused(
        lambda: client.call("POST", "/v1/host/results", {"results": [result], "ack": 2})
    )
    assert err.status == 400
    assert (relay.results, relay.acks) == ([], [])  # the whole batch, refused
    assert client.call("POST", "/v1/host/results", {"results": [result], "ack": 1}) == {
        "accepted": 1
    }
    assert (relay.acks, relay.acked_seq) == ([1], 1)
    assert client.call("POST", "/v1/host/results", {"results": [result]}) == {
        "accepted": 0
    }


def test_ack_ahead_names_its_reason(relay):
    conn, resp = _raw(relay, "POST", "/v1/host/ack", body=b'{"seq": 1}')
    try:
        error = json.loads(resp.read())["error"]
        assert (resp.status, error["details"]) == (400, {"reason": "ack_ahead"})
    finally:
        conn.close()


def test_rate_limits_hit_only_the_routes_the_relay_limits(relay):
    waits: list[float] = []
    client = _host(relay, sleep=waits.append)
    relay.queue_command(_command(5))
    relay.rate_limit_next_calls(2, retry_after=3)
    # retries=0: a limit on ack would raise here instead of being retried away.
    unlimited = _host(relay, retries=0)
    assert unlimited.call("POST", "/v1/host/ack", {"seq": 5}) == {"seq": 5}
    assert unlimited.call("GET", "/v1/host/devices") == {"devices": []}
    assert "commands" in unlimited.call("GET", "/v1/host/commands")
    assert client.call("POST", "/v1/host/events", {"events": [EVENT]})["accepted"] == 1
    assert (waits, relay.events) == ([3.0, 3.0], [EVENT])  # refused twice, applied once
    paths = [path for _, path, _ in relay.requests]
    assert paths.count("/v1/host/ack") == 1
    assert paths.count("/v1/host/events") == 3
    relay.rate_limit_next_calls(1, retry_after=4)
    err = _refused(
        lambda: _host(relay, retries=0).call("PUT", "/v1/host/snapshot", SNAPSHOT)
    )
    assert (err.status, err.code, err.retry_after) == (429, "rate_limited", 4.0)
    assert relay.snapshots == []


# -- the stream ----------------------------------------------------------------


def test_the_stream_headers_and_its_first_bytes(relay):
    conn, resp = _raw(relay, "GET", "/v1/stream/host")
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/event-stream"
        assert resp.getheader("Cache-Control") == "no-cache, no-transform"
        assert resp.getheader("X-Accel-Buffering") == "no"
        assert resp.read(6) == b": ok\n\n"
    finally:
        conn.close()


def test_frames_are_event_then_id_then_data(relay):
    relay.queue_command(COMMAND)
    conn, resp = _raw(relay, "GET", "/v1/stream/host")
    try:
        lines = [resp.readline() for _ in range(9)]
    finally:
        conn.close()
    devices = lines.index(b"event: devices\n")
    assert lines[devices + 1] == b'data: {"devices": []}\n'
    command = lines.index(b"event: command\n")
    assert lines[command + 1] == b"id: 1\n"
    assert lines[command + 2] == b"data: " + json.dumps(COMMAND).encode() + b"\n"
    assert devices < command


def test_a_heartbeat_pings_a_quiet_stream():
    with FakeRelay(heartbeat_s=0.1) as fake:
        conn, resp = _raw(fake, "GET", "/v1/stream/host")
        try:
            lines = [resp.readline() for _ in range(6)]
        finally:
            conn.close()
    assert b": ping\n" in lines


def test_every_connect_replays_devices_then_commands_then_uploads():
    with FakeRelay(devices=[DEVICE]) as fake:
        fake.queue_upload_ready(
            {"uploadId": UPLOAD_ID, "getUrl": f"{fake.url}/blob/u1"}
        )
        fake.queue_command(_command(1))
        fake.queue_command(_command(2))
        client = _host(fake)
        for _ in range(2):  # a reconnect replays it all again
            events = _take(client, 4, "/v1/stream/host?since=0")
            assert [e.event for e in events] == [
                "devices",
                "command",
                "command",
                "upload.ready",
            ]
            assert json.loads(events[0].data) == {"devices": [DEVICE]}
            assert [e.id for e in events[1:3]] == ["1", "2"]


def test_the_stream_replays_unresolved_commands_after_since(relay):
    relay.queue_command(COMMAND)
    relay.queue_command(_command(2, C2))
    stream = _host(relay).stream("/v1/stream/host?since=1", read_timeout=5)
    assert next(stream).event == "devices"
    first = next(stream)
    assert (first.event, first.id, json.loads(first.data)["id"]) == (
        "command",
        "2",
        C2,
    )
    relay.drop_streams()
    assert list(stream) == []


def test_a_bad_since_on_the_stream_replays_from_zero(relay):
    # As at the relay: not a decimal -> Last-Event-ID, then 0. No 400.
    relay.queue_command(COMMAND)
    events = _take(_host(relay), 2, "/v1/stream/host?since=abc")
    assert [(e.event, e.id) for e in events] == [("devices", None), ("command", "1")]


def test_a_since_with_a_trailing_newline_is_not_a_since(relay):
    relay.queue_command(COMMAND)
    events = _take(_host(relay), 2, "/v1/stream/host?since=1%0A")
    assert [(e.event, e.id) for e in events] == [("devices", None), ("command", "1")]


def test_a_resulted_command_is_not_replayed(relay):
    relay.queue_command(COMMAND)
    client = _host(relay)
    result = {
        "type": "command",
        "commandId": COMMAND["id"],
        "status": 200,
        "sealed": "x" * 40,
    }
    client.call("POST", "/v1/host/results", {"results": [result]})
    relay.queue_command(_command(2, C2))
    events = _take(client, 2)
    assert [(e.event, e.id) for e in events] == [("devices", None), ("command", "2")]


def test_devices_upload_ready_and_revoked_reach_an_open_stream(relay):
    client = _host(relay)
    stream = client.stream("/v1/stream/host", read_timeout=5)
    assert json.loads(next(stream).data) == {"devices": []}
    relay.set_devices([DEVICE])
    assert json.loads(next(stream).data) == {"devices": [DEVICE]}
    relay.queue_upload_ready({"uploadId": UPLOAD_ID, "getUrl": f"{relay.url}/blob/u1"})
    assert next(stream).event == "upload.ready"
    relay.revoke()
    assert next(stream).event == "revoked"
    assert list(stream) == []
    with pytest.raises(Revoked):
        client.call("GET", "/v1/host/devices")


def test_a_host_holds_at_most_two_streams(relay):
    client = _host(relay)
    streams = [
        client.stream("/v1/stream/host", read_timeout=5)
        for _ in range(HOST_STREAM_LIMIT)
    ]
    for stream in streams:
        assert next(stream).event == "devices"
    err = _refused(lambda: list(client.stream("/v1/stream/host", read_timeout=5)))
    assert (err.status, err.code, err.retry_after) == (429, "rate_limited", 5.0)
    streams[0].close()  # the relay notices the hang-up and frees the slot
    relay.wait_for(lambda: relay.open_streams == 1)
    assert [e.event for e in _take(client, 1)] == ["devices"]
    streams[1].close()


def test_scripted_connect_failures(relay):
    relay.fail_next_connects(1, 503)
    client = _host(relay)
    err = _refused(lambda: list(client.stream("/v1/stream/host", read_timeout=5)))
    assert (err.status, err.code) == (503, "unavailable")
    relay.fail_next_connects(1, 429, retry_after=7)
    err = _refused(lambda: list(client.stream("/v1/stream/host", read_timeout=5)))
    assert (err.status, err.code, err.retry_after) == (429, "rate_limited", 7.0)
    assert [e.event for e in _take(client, 1)] == ["devices"]


# -- long-poll and blobs -------------------------------------------------------


def test_long_poll_and_blob(relay):
    relay.queue_command(COMMAND)
    relay.objects["u1"] = b"cipher"
    client = _host(relay)
    polled = client.call("GET", "/v1/host/commands?since=0&wait=25")
    assert [c["id"] for c in polled["commands"]] == [COMMAND["id"]]
    got = bytearray()
    assert client.get_bytes(f"{relay.url}/blob/u1", got.extend, max_bytes=100) == 6
    assert bytes(got) == b"cipher"


def test_the_long_poll_since_defaults_to_the_acked_seq(relay):
    relay.queue_command(_command(1))
    relay.queue_command(_command(2))
    client = _host(relay)
    client.call("POST", "/v1/host/ack", {"seq": 1})
    polled = client.call("GET", "/v1/host/commands")
    assert [c["seq"] for c in polled["commands"]] == [2]


def test_the_long_poll_waits_for_a_command_when_asked_to():
    with FakeRelay(long_poll_s=2) as fake:
        client = _host(fake)
        started = time.monotonic()
        assert client.call("GET", "/v1/host/commands?wait=1")["commands"] == []
        assert 0.9 <= time.monotonic() - started < 2  # the request's own wait wins
        threading.Timer(0.2, fake.queue_command, [COMMAND]).start()
        polled = client.call("GET", "/v1/host/commands?wait=25")
        assert [c["id"] for c in polled["commands"]] == [COMMAND["id"]]


# -- pairing -------------------------------------------------------------------


def _poll(client: RelayClient, start: dict[str, object]) -> dict[str, object]:
    path = "/v1/host/pair/poll?pairingId=" + str(start["pairingId"])
    return client.call("GET", path, token=str(start["pollToken"]))


def test_pairing_is_pending_then_claimed(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    assert start["code"] == PAIRING_CODE and start["appUrl"] == relay.url
    assert relay.pair_starts == [PAIR_START]
    relay.stage_pairing(claimed_devices=[{"deviceId": "dev_1"}], after_polls=2)
    assert _poll(client, start)["status"] == "pending"
    claimed = _poll(client, start)
    assert claimed == {
        "status": "claimed",
        "hostId": "hst_fake_1",
        "hostToken": relay.host_token,
        "devices": [{"deviceId": "dev_1"}],
    }


def test_pair_start_sends_the_code_dashed_as_the_relay_does(relay):
    start = RelayClient(relay.url, timeout=5).call(
        "POST", "/v1/host/pair/start", PAIR_START
    )
    code = str(start["code"])
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert all(ch in CODE_ALPHABET for ch in code.replace("-", ""))
    assert not set("01IO") & set(CODE_ALPHABET)


@pytest.mark.parametrize(
    ("typed", "bare"),
    [
        ("ABCD-EFGH", "ABCDEFGH"),
        ("ABCDEFGH", "ABCDEFGH"),
        ("abcd-efgh", "ABCDEFGH"),
        (" abcd efgh\n", "ABCDEFGH"),
        ("AB-CD-EF-GH", "ABCDEFGH"),
        ("ABCD-EFG", None),
        ("ABCD-EFGHJ", None),
        ("ABCD-EFG0", None),
        ("ABCD-EFGI", None),
        ("ABCD_EFGH", None),
    ],
)
def test_a_code_reads_back_with_or_without_the_dash(typed, bare):
    assert normalize_code(typed) == bare
    assert normalize_code(PAIRING_CODE) == "ABCDEFGH"


@pytest.mark.parametrize(
    "ident",
    ["00000000-0000-0000-0000-000000000000", "FFFFFFFF-ffff-ffff-ffff-ffffffffffff"],
)
def test_the_nil_and_max_uuids_are_uuids(relay, ident):
    relay.queue_command(_command(1, ident))
    result = {"type": "command", "commandId": ident, "status": 200, "sealed": SEALED}
    assert _host(relay).call("POST", "/v1/host/results", {"results": [result]}) == {
        "accepted": 1
    }
    upload = {"type": "upload", "uploadId": ident, "state": "pulled"}
    assert _host(relay).call("POST", "/v1/host/results", {"results": [upload]}) == {
        "accepted": 0
    }


@pytest.mark.parametrize(
    "ident", ["cmd_DECOY_4", "c2", "0190f3a2-7b1c-0d4e-8a5b-3c2d1e0f9a8c", None]
)
def test_scripting_refuses_an_id_that_is_not_a_uuid(relay, ident):
    with pytest.raises(ValueError, match="not a UUID"):
        relay.queue_command({**COMMAND, "id": ident})
    with pytest.raises(ValueError, match="not a UUID"):
        relay.queue_upload_ready({"uploadId": ident, "getUrl": f"{relay.url}/blob/u1"})
    assert _host(relay).call("GET", "/v1/host/commands") == {
        "commands": [],
        "uploads": [],
    }


def test_a_stream_client_that_never_reads_frees_its_slot(monkeypatch):
    # A blocked write gives up after the server's socket timeout, so a client
    # that stopped reading cannot hold a stream slot forever.
    monkeypatch.setattr(_Handler, "timeout", 1.0)
    with FakeRelay() as fake:
        for seq in range(1, 33):
            fake.queue_command({**_command(seq), "sealed": "z" * 1_000_000})
        conn, resp = _raw(fake, "GET", "/v1/stream/host")
        try:
            assert resp.status == 200
            fake.wait_for(lambda: fake.open_streams == 0, timeout=15)
        finally:
            conn.close()


def test_the_host_token_is_handed_out_once(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    relay.stage_pairing(claimed_devices=[{"deviceId": "dev_1"}])
    assert "hostToken" in _poll(client, start)
    again = _poll(client, start)
    assert again["status"] == "claimed" and "hostToken" not in again


def test_a_claimed_pairing_never_turns_expired(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    relay.stage_pairing(claimed_devices=[{"deviceId": "dev_1"}])
    assert _poll(client, start)["status"] == "claimed"
    relay.expire_pairing()
    assert _poll(client, start)["status"] == "claimed"


def test_an_unclaimed_pairing_expires(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    relay.expire_pairing()
    assert _poll(client, start) == {"status": "expired"}


def test_a_claim_adds_its_devices_to_the_trusted_list(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    paired = {k: DEVICE[k] for k in ("deviceId", "name", "kexPub", "signPub")}
    relay.stage_pairing(claimed_devices=[paired])
    _poll(client, start)
    assert relay.devices == [
        {**paired, "pairedAt": "2026-10-08T22:00:00.000Z", "revokedAt": None}
    ]
    assert relay.host_keys == (KEX, SIGN)


def test_pair_poll_needs_the_pairing_id_and_its_poll_token(relay):
    client = RelayClient(relay.url, timeout=5)
    start = client.call("POST", "/v1/host/pair/start", PAIR_START)
    err = _refused(
        lambda: client.call("GET", "/v1/host/pair/poll", token=str(start["pollToken"]))
    )
    assert (err.status, err.code) == (400, "invalid_request")
    with pytest.raises(Revoked):
        client.call(
            "GET", "/v1/host/pair/poll?pairingId=other", token=str(start["pollToken"])
        )
    with pytest.raises(Revoked):
        client.call(
            "GET", "/v1/host/pair/poll?pairingId=pair_fake_1", token=relay.host_token
        )


def test_the_pair_poll_long_polls_when_asked_to():
    with FakeRelay(long_poll_s=2) as fake:
        client = RelayClient(fake.url, timeout=5)
        start = client.call("POST", "/v1/host/pair/start", PAIR_START)
        threading.Timer(0.2, fake.stage_pairing, kwargs={"claimed_devices": []}).start()
        assert _poll(client, start)["status"] == "claimed"


def test_pair_start_validates_its_body(relay):
    err = _refused(
        lambda: RelayClient(relay.url, timeout=5).call(
            "POST", "/v1/host/pair/start", {"name": "box"}
        )
    )
    assert (err.status, err.code) == (400, "invalid_request")
    assert relay.pair_starts == []


def test_an_add_device_pair_start_checks_the_token_and_the_keys():
    with FakeRelay(host_keys=(KEX, SIGN)) as fake:
        with pytest.raises(Revoked):
            RelayClient(fake.url, token="mlh_DECOY_wrong", timeout=5).call(
                "POST", "/v1/host/pair/start", PAIR_START
            )
        client = _host(fake)
        err = _refused(
            lambda: client.call(
                "POST", "/v1/host/pair/start", {**PAIR_START, "hostKexPub": "Q" * 43}
            )
        )
        assert (err.status, err.code) == (409, "conflict")
        assert (
            client.call("POST", "/v1/host/pair/start", PAIR_START)["pairingId"]
            == "pair_fake_1"
        )
        assert fake.pair_starts == [PAIR_START]


def test_a_key_mismatch_names_its_reason():
    with FakeRelay(host_keys=(KEX, SIGN)) as fake:
        body = json.dumps({**PAIR_START, "hostSignPub": "Q" * 43}).encode()
        conn, resp = _raw(fake, "POST", "/v1/host/pair/start", body=body)
        try:
            error = json.loads(resp.read())["error"]
        finally:
            conn.close()
    assert (resp.status, error["details"]) == (409, {"reason": "key_mismatch"})


# -- unlink and helpers --------------------------------------------------------


def test_unlink_revokes(relay):
    client = _host(relay)
    assert client.call("DELETE", "/v1/host") == {"revoked": True}
    assert relay.deleted
    with pytest.raises(Revoked):
        client.call("GET", "/v1/host/devices")


def test_wait_for_times_out_loudly(relay):
    with pytest.raises(AssertionError, match="fake relay"):
        relay.wait_for(lambda: False, timeout=0.1)
