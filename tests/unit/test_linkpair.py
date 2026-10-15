"""linkpair: start, wait, and accept a pairing over the (fake) relay."""

from __future__ import annotations

import dataclasses
import inspect
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from magent import env, linkpair
from magent import linkcrypto as lc
from magent.link import RelayClient, RelayError
from tests._fake_relay import CODE_ALPHABET, PAIRING_CODE, FakeRelay, normalize_code

if TYPE_CHECKING:
    from collections.abc import Callable

HOST = lc.generate_key_pairs()
DEVICE = lc.generate_key_pairs()
DEVICE_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8b"
ESC = chr(27)
NEWLINE = chr(10)


def _pin(monkeypatch: pytest.MonkeyPatch, relay_url: str, app_url: str) -> None:
    """Point MAGENT_LINK_URL / MAGENT_LINK_APP_URL at a test relay and app (the
    conftest ``fake_relay`` pattern, plus the app URL a pairing must match)."""
    monkeypatch.setenv("MAGENT_LINK_URL", relay_url)
    monkeypatch.setenv("MAGENT_LINK_APP_URL", app_url)
    monkeypatch.setattr(env, "_cached_env", None)


@pytest.fixture
def relay(monkeypatch):
    with FakeRelay() as r:
        _pin(monkeypatch, r.url, r.url)  # the fake's appUrl is its own address
        yield r


def _start(relay: FakeRelay, **kwargs: object) -> linkpair.PairingStart:
    client = RelayClient(relay.url, timeout=5)
    return linkpair.start_pairing(
        client, HOST, name="box", platform="win32", version="3.21.0", **kwargs
    )


def _device(start: linkpair.PairingStart | None) -> dict[str, object]:
    raw: dict[str, object] = {
        "deviceId": DEVICE_ID,
        "name": "Pixel",
        "kexPub": lc.b64u_encode(DEVICE.kex_pub),
        "signPub": lc.b64u_encode(DEVICE.sign_pub),
    }
    if start is not None:
        raw["mac"] = lc.pair_mac(start.secret, DEVICE.kex_pub, DEVICE.sign_pub)
    return raw


def _wait(relay: FakeRelay, start: linkpair.PairingStart) -> dict[str, object]:
    client = RelayClient(relay.url, timeout=5)
    return linkpair.wait_for_claim(
        client, start, deadline=float("inf"), sleep=lambda s: None
    )


def _accept(
    relay: FakeRelay,
    poll: dict[str, object],
    start: linkpair.PairingStart,
    confirm: Callable[[str], bool] = lambda n: True,
) -> linkpair.ClaimOutcome:
    client = RelayClient(relay.url, timeout=5)
    return linkpair.accept_claim(poll, start, HOST, confirm=confirm, client=client)


def _no_prompt(number: str) -> bool:
    pytest.fail("asked about a claim that cannot pair")


def _claim(relay: FakeRelay, *devices: dict[str, object]) -> dict[str, object]:
    """A new-host claim as the relay's first claimed poll reply carries it."""
    return {
        "status": "claimed",
        "hostId": "hst_fake_1",
        "hostToken": relay.host_token,
        "devices": list(devices),
    }


def _deletes(relay: FakeRelay) -> list[str]:
    return [h.get("Authorization", "") for m, p, h in relay.requests if m == "DELETE"]


def test_start_cuts_its_strings_as_the_relay_counts(relay):
    # The relay trims the name, then counts UTF-16 code units: an emoji is
    # two. Cut by code points, this start would be refused with a 400.
    client = RelayClient(relay.url, timeout=5)
    emoji = chr(0x1F600)
    linkpair.start_pairing(
        client,
        HOST,
        name="  " + emoji * 150 + "  ",
        platform=emoji * 20,
        version="a" + emoji * 20,
    )
    sent = relay.pair_starts[-1]
    assert sent["name"] == emoji * 50  # 100 code units
    assert sent["platform"] == emoji * 16
    assert sent["daemonVersion"] == "a" + emoji * 15


def test_start_sends_the_public_keys_and_reads_the_reply(relay):
    start = _start(relay)
    assert relay.pair_starts == [
        {
            "hostKexPub": lc.b64u_encode(HOST.kex_pub),
            "hostSignPub": lc.b64u_encode(HOST.sign_pub),
            "name": "box",
            "platform": "win32",
            "daemonVersion": "3.21.0",
        }
    ]
    assert (start.pairing_id, start.code, start.poll_token) == (
        "pair_fake_1",
        normalize_code(PAIRING_CODE),  # the relay answers it dashed
        relay.poll_token,
    )
    assert start.app_url == relay.url and len(start.secret) == 16


def test_add_device_sends_the_host_token(relay):
    _start(relay, host_token=relay.host_token)
    _, path, headers = relay.requests[-1]
    assert path == "/v1/host/pair/start"
    assert headers["Authorization"] == f"Bearer {relay.host_token}"


def test_display_code_and_qr_url(relay):
    start = _start(relay)
    bare = normalize_code(PAIRING_CODE)
    assert linkpair.display_code(start.code) == PAIRING_CODE
    url = linkpair.qr_url(start, HOST)
    assert url == (
        f"{relay.url}/pair#c={bare}&k={lc.fingerprint(HOST.kex_pub, HOST.sign_pub)}"
        f"&s={lc.b64u_encode(start.secret)}"
    )


def test_the_qr_path_pairs_without_a_prompt(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(start)], after_polls=2)
    poll = _wait(relay, start)
    outcome = _accept(relay, poll, start, _no_prompt)
    assert (
        outcome.via_qr
        and outcome.host_id == "hst_fake_1"
        and outcome.host_token == relay.host_token
    )
    (device,) = outcome.devices
    assert (device.device_id, device.name) == (DEVICE_ID, "Pixel")
    assert device.kex_pub == lc.b64u_encode(DEVICE.kex_pub)
    assert device.sign_pub == lc.b64u_encode(DEVICE.sign_pub)
    assert device.paired_at.endswith("+00:00")
    polls = [p for _, p, _ in relay.requests if p.startswith("/v1/host/pair/poll")]
    assert polls == ["/v1/host/pair/poll?pairingId=pair_fake_1"] * 2


def test_a_wrong_mac_pairs_nothing(relay):
    start = _start(relay)
    bad = {
        **_device(start),
        "mac": lc.pair_mac(bytes(16), DEVICE.kex_pub, DEVICE.sign_pub),
    }
    relay.stage_pairing(claimed_devices=[bad])
    with pytest.raises(linkpair.PairingRejected):
        _accept(relay, _wait(relay, start), start)


def test_the_typed_path_asks_for_the_safety_number(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(None)])
    asked: list[str] = []

    def confirm(number: str) -> bool:
        asked.append(number)
        return True

    outcome = _accept(relay, _wait(relay, start), start, confirm)
    assert asked == [
        lc.safety_number(HOST.kex_pub, HOST.sign_pub, DEVICE.kex_pub, DEVICE.sign_pub)
    ]
    assert not outcome.via_qr


def test_a_declined_safety_number_pairs_nothing(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(None)])
    with pytest.raises(linkpair.PairingRejected):
        _accept(relay, _wait(relay, start), start, lambda n: False)


def test_add_device_returns_no_token(relay):
    start = _start(relay, host_token=relay.host_token)
    relay.stage_pairing(claimed_devices=[_device(start)], new_host=False)
    outcome = _accept(relay, _wait(relay, start), start)
    assert outcome.host_token is None


def test_an_expired_code(relay):
    start = _start(relay)
    relay.expire_pairing()
    with pytest.raises(linkpair.PairingExpired):
        _wait(relay, start)


def test_the_deadline_stops_waiting_without_polling(relay):
    start = _start(relay)
    client = RelayClient(relay.url, timeout=5)
    before = len(relay.requests)
    with pytest.raises(linkpair.PairingExpired):
        linkpair.wait_for_claim(
            client, start, deadline=10.0, clock=lambda: 11.0, sleep=lambda s: None
        )
    assert len(relay.requests) == before


@pytest.mark.parametrize(
    "poll",
    [
        {"status": "claimed", "hostId": "h", "devices": []},
        {"status": "claimed", "devices": [{"deviceId": "d"}]},
        {
            "status": "claimed",
            "hostId": "h",
            "devices": [
                {"deviceId": "d", "name": "n", "kexPub": "short", "signPub": "short"}
            ],
        },
    ],
)
def test_a_malformed_claim_is_refused(relay, poll):
    start = _start(relay)
    with pytest.raises(RelayError):
        _accept(relay, poll, start)


# -- the start reply: code, appUrl, expiry ---------------------------------------


def _replying(
    monkeypatch: pytest.MonkeyPatch, base: str, **changes: object
) -> RelayClient:
    """A client whose every call answers a pair/start reply (plus ``changes``)."""
    reply: dict[str, object] = {
        "pairingId": "pair_fake_1",
        "code": "ABCD-EFGH",
        "pollToken": "mlp_DECOY_x",
        "expiresAt": "2026-10-08T22:10:00.000Z",
        "appUrl": "https://app.example",
        **changes,
    }
    monkeypatch.setattr(RelayClient, "call", lambda self, *a, **k: reply)
    return RelayClient(base, timeout=5)


def _start_with(client: RelayClient) -> linkpair.PairingStart:
    return linkpair.start_pairing(
        client, HOST, name="box", platform="win32", version="3.21.0"
    )


@pytest.mark.parametrize("code", ["ABCD-EFGH", "abcd-efgh", " ABCD EFGH ", "ABCDEFGH"])
def test_the_relay_s_dashed_code_is_stored_undashed(monkeypatch, code):
    _pin(monkeypatch, "https://relay.example", "https://app.example")
    start = _start_with(_replying(monkeypatch, "https://relay.example", code=code))
    assert start.code == "ABCDEFGH" == normalize_code(code)  # agrees with the relay
    assert linkpair.display_code(start.code) == "ABCD-EFGH"
    assert "#c=ABCDEFGH&" in linkpair.qr_url(start, HOST)


def test_the_code_alphabet_is_the_relay_s():
    assert frozenset(CODE_ALPHABET) == linkpair.CODE_ALPHABET


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("code", "ABCD0FGH"),
        ("code", "ABCDEFG"),
        ("code", "ABCD-EFGH-J"),
        ("code", "ABCD&s=x"),
        ("expiresAt", "tomorrow"),
        ("expiresAt", "2026-10-08T22:10:00"),
    ],
)
def test_a_malformed_start_reply_is_refused(monkeypatch, key, value):
    _pin(monkeypatch, "https://relay.example", "https://app.example")
    client = _replying(monkeypatch, "https://relay.example", **{key: value})
    with pytest.raises(RelayError):
        _start_with(client)


@pytest.mark.parametrize(
    "app_url",
    ["https://app.example", "https://app.example/", "https://APP.example:443"],
)
def test_the_configured_app_is_accepted(monkeypatch, app_url):
    _pin(monkeypatch, "https://relay.example", "https://app.example")
    start = _start_with(_replying(monkeypatch, "https://relay.example", appUrl=app_url))
    assert start.app_url == app_url


@pytest.mark.parametrize(
    "app_url",
    [
        "https://relay.example@evil.example",
        "https://user:pw@app.example",
        "https://evil.example",
        "https://app.example.evil.example",
        "http://app.example",
        "https://app.example:8443",
        "https://app.example:0",
        "https://app.example/?x=1",
        "https://app.example/?",
        "https://app.example#k=x",
        "https://app.example/#",
        "https://app.example" + chr(92) + "@evil.example",
        "https://app.example/ x",
        "javascript:alert(1)",
        "https://app.exampl" + chr(0xE9),
    ],
)
def test_an_app_url_other_than_the_configured_one_is_refused(monkeypatch, app_url):
    _pin(monkeypatch, "https://relay.example", "https://app.example")
    client = _replying(monkeypatch, "https://relay.example", appUrl=app_url)
    with pytest.raises(RelayError, match="appUrl"):
        _start_with(client)


def test_http_app_url_only_behind_an_http_relay(monkeypatch):
    _pin(monkeypatch, "http://127.0.0.1:9", "http://127.0.0.1:3950")
    start = _start_with(
        _replying(monkeypatch, "http://127.0.0.1:9", appUrl="http://127.0.0.1:3950")
    )
    assert start.app_url == "http://127.0.0.1:3950"
    _pin(monkeypatch, "https://relay.example", "http://127.0.0.1:3950")
    https_relay = _replying(
        monkeypatch, "https://relay.example", appUrl="http://127.0.0.1:3950"
    )
    with pytest.raises(RelayError, match="appUrl"):
        _start_with(https_relay)


def test_the_app_url_defaults_to_app_magent_now(monkeypatch):
    monkeypatch.delenv("MAGENT_LINK_APP_URL", raising=False)
    monkeypatch.setattr(env, "_cached_env", None)
    client = _replying(
        monkeypatch, "https://relay.example", appUrl="https://app.magent.now"
    )
    assert _start_with(client).app_url == "https://app.magent.now"
    other = _replying(
        monkeypatch, "https://relay.example", appUrl="https://app.example"
    )
    with pytest.raises(RelayError, match="appUrl"):
        _start_with(other)


def test_the_deadline_is_the_code_expiry(relay):
    start = _start(relay)
    expected = datetime(2026, 10, 8, 22, 10, tzinfo=timezone.utc).timestamp()
    assert linkpair.deadline_of(start) == expected
    with pytest.raises(RelayError):
        linkpair.deadline_of(dataclasses.replace(start, expires_at="soon"))


# -- 429s: start never waits; the poll waits on its own clock --------------------


def _limit_first_call(monkeypatch: pytest.MonkeyPatch, retry_after: int) -> None:
    """The client's first exchange answers 429; later ones reach the fake.

    Scripted here: the relay does not rate-limit pair/poll, so the fake has
    no 429 for it, but a proxy in front of the relay still can answer one and
    the wait must ride it out.
    """
    real = RelayClient._exchange
    calls: list[str] = []
    refusal = {"ok": False, "error": {"code": "rate_limited", "message": "Slow down."}}

    def exchange(self, method, path, payload, headers, timeout):
        calls.append(path)
        if len(calls) == 1:
            return 429, json.dumps(refusal).encode(), float(retry_after)
        return real(self, method, path, payload, headers, timeout)

    monkeypatch.setattr(RelayClient, "_exchange", exchange)


@pytest.mark.parametrize(
    ("retry_after", "words"), [(3, "1 min"), (60, "1 min"), (3600, "60 min")]
)
def test_a_rate_limited_start_raises_at_once_on_a_default_client(
    relay, retry_after, words
):
    relay.rate_limit_next_calls(1, retry_after=retry_after)
    slept: list[float] = []
    client = RelayClient(relay.url, timeout=5, sleep=slept.append)
    with pytest.raises(linkpair.PairingRateLimited) as exc:
        _start_with(client)
    assert exc.value.retry_after == retry_after
    assert f"try again in {words}" in str(exc.value)
    assert slept == [] and relay.pair_starts == []


def test_a_rate_limited_poll_resumes_after_retry_after(relay, monkeypatch):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(start)])
    _limit_first_call(monkeypatch, 7)
    client_slept: list[float] = []
    slept: list[float] = []
    client = RelayClient(relay.url, timeout=5, sleep=client_slept.append)
    poll = linkpair.wait_for_claim(
        client, start, deadline=float("inf"), sleep=slept.append
    )
    assert poll["status"] == "claimed" and slept == [7] and client_slept == []


def test_a_rate_limited_poll_never_sleeps_past_the_deadline(relay, monkeypatch):
    start = _start(relay)
    _limit_first_call(monkeypatch, 30)
    client_slept: list[float] = []
    slept: list[float] = []
    now = iter([100.0, 100.0, 104.0, 104.0])
    client = RelayClient(relay.url, timeout=5, sleep=client_slept.append)
    with pytest.raises(linkpair.PairingExpired):
        linkpair.wait_for_claim(
            client, start, deadline=104.0, clock=lambda: next(now), sleep=slept.append
        )
    assert slept == [4.0] and client_slept == []


def test_the_pending_pause_never_runs_past_the_deadline(relay):
    start = _start(relay)
    slept: list[float] = []
    now = iter([100.0, 100.0, 101.0])
    client = RelayClient(relay.url, timeout=5)
    with pytest.raises(linkpair.PairingExpired):
        linkpair.wait_for_claim(
            client, start, deadline=100.5, clock=lambda: next(now), sleep=slept.append
        )
    assert slept == [0.5]


def test_other_relay_errors_end_the_wait(relay):
    start = dataclasses.replace(_start(relay), poll_token="mlp_DECOY_wrong")
    with pytest.raises(RelayError) as exc:
        _wait(relay, start)
    assert exc.value.status == 401


@pytest.mark.parametrize(
    ("retry_after", "words"),
    [(None, "in a few minutes"), (1, "in 1 min"), (61, "in 2 min")],
)
def test_the_rate_limit_message_rounds_up_to_minutes(retry_after, words):
    assert str(linkpair.PairingRateLimited(retry_after)).endswith(f"try again {words}")


def test_secrets_stay_out_of_repr(relay):
    start = _start(relay)
    text = repr(start)
    assert (
        start.poll_token not in text
        and repr(start.secret) not in text
        and "secret" not in text
    )
    relay.stage_pairing(claimed_devices=[_device(start)])
    outcome = _accept(relay, _wait(relay, start), start)
    assert outcome.host_token == relay.host_token
    assert relay.host_token not in repr(outcome)


# -- the claim: one well-formed device, decided once ------------------------------


def test_client_is_a_required_argument():
    parameter = inspect.signature(linkpair.accept_claim).parameters["client"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("shape", ["none", "duplicate", "mixed", "two"])
def test_a_claim_must_name_exactly_one_device(relay, shape):
    start = _start(relay)
    qr, typed = _device(start), _device(None)
    other = {**typed, "deviceId": "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8c"}
    devices = {
        "none": [],
        "duplicate": [qr, qr],
        "mixed": [qr, typed],
        "two": [typed, other],
    }[shape]
    with pytest.raises(RelayError, match="exactly one device"):
        _accept(relay, _claim(relay, *devices), start, _no_prompt)
    assert _deletes(relay) == [f"Bearer {relay.host_token}"]


@pytest.mark.parametrize(
    "device_id",
    [
        "d2",
        "0190F3A2-7B1C-7D4E-8A5B-3C2D1E0F9A8B",
        "{0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8b}",
        "0190f3a27b1c7d4e8a5b3c2d1e0f9a8b",
        "urn:uuid:0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8b",
        "../../x",
    ],
)
def test_a_device_id_must_be_a_canonical_uuid(relay, device_id):
    start = _start(relay)
    device = {**_device(None), "deviceId": device_id}
    with pytest.raises(RelayError, match="deviceId"):
        _accept(relay, _claim(relay, device), start, _no_prompt)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(ESC + "[2J" + ESC + "]0;pwned" + chr(7), id="ansi-escape"),
        pytest.param("../../x" + NEWLINE, id="path-and-newline"),
        pytest.param("a" * 101, id="101-chars"),
        pytest.param(chr(0x1F600) * 51, id="51-emoji"),  # 102 UTF-16 units
        pytest.param("a" * 5000, id="5000-chars"),
        pytest.param("Pix" + chr(0x200B) + "el", id="zero-width"),
        pytest.param("Pixel" + chr(0x202E) + "lexiP", id="bidi-override"),
        pytest.param("Pixel" + chr(0xE000), id="private-use"),
        pytest.param("Pixel" + chr(0xD800), id="lone-surrogate"),
        pytest.param(42, id="not-text"),
    ],
)
def test_a_device_name_must_be_short_printable_text(relay, name):
    start = _start(relay)
    device = {**_device(None), "name": name}
    with pytest.raises(RelayError, match="device name"):
        _accept(relay, _claim(relay, device), start, _no_prompt)


@pytest.mark.parametrize(
    "name",
    [
        "a" * 100,
        chr(0x1F600) * 50,  # 100 UTF-16 units
        "Pixel 8 Pr" + chr(0xF3),
        "x",
        "Pixel " + chr(0x1FAE0),  # Unicode 14: unassigned (Cn) to Python 3.10
        "Pixel " + chr(0x1FAE9),  # Unicode 16: unassigned to Python 3.13
    ],
)
def test_a_plain_device_name_is_kept(relay, name):
    start = _start(relay)
    outcome = _accept(relay, _claim(relay, {**_device(start), "name": name}), start)
    assert outcome.devices[0].name == name


@pytest.mark.parametrize(
    "mac",
    [
        pytest.param("", id="empty"),
        pytest.param(chr(0xE9) * 43, id="non-ascii"),
        pytest.param("A" * 42, id="42-chars"),
        pytest.param("A" * 44, id="44-chars"),
        pytest.param("A" * 42 + "+", id="not-b64url"),
        pytest.param("A" * 42 + "B", id="non-canonical-tail"),
        pytest.param(5, id="not-text"),
    ],
)
def test_a_malformed_mac_is_refused_not_read_as_typed(relay, mac):
    start = _start(relay)
    device = {**_device(None), "mac": mac}
    with pytest.raises(RelayError, match="bad mac"):
        _accept(relay, _claim(relay, device), start, _no_prompt)


def test_a_null_mac_is_the_typed_path(relay):
    start = _start(relay)
    outcome = _accept(relay, _claim(relay, {**_device(None), "mac": None}), start)
    assert not outcome.via_qr


def test_a_wrong_mac_never_falls_back_to_the_safety_number(relay):
    start = _start(relay)
    bad = {
        **_device(start),
        "mac": lc.pair_mac(b"x" * 16, DEVICE.kex_pub, DEVICE.sign_pub),
    }
    relay.stage_pairing(claimed_devices=[bad])
    with pytest.raises(linkpair.PairingRejected, match="QR proof"):
        _accept(relay, _wait(relay, start), start, _no_prompt)


def test_the_mac_covers_exactly_the_trusted_keys(relay):
    start = _start(relay)
    other = lc.generate_key_pairs()
    swapped = {
        **_device(start),
        "kexPub": lc.b64u_encode(other.kex_pub),  # mac still over DEVICE's keys
    }
    with pytest.raises(linkpair.PairingRejected, match="QR proof"):
        _accept(relay, _claim(relay, swapped), start, _no_prompt)


def test_what_is_trusted_is_what_was_verified(relay):
    start = _start(relay)
    poll = _claim(relay, _device(None))
    other = lc.generate_key_pairs()

    def confirm(number: str) -> bool:
        claimed = poll["devices"]
        assert isinstance(claimed, list)
        claimed[0]["kexPub"] = lc.b64u_encode(other.kex_pub)
        claimed[0]["signPub"] = lc.b64u_encode(other.sign_pub)
        claimed[0]["deviceId"] = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8c"
        claimed[0]["name"] = "Evil"
        return True

    (device,) = _accept(relay, poll, start, confirm).devices
    assert (device.device_id, device.name) == (DEVICE_ID, "Pixel")
    assert device.kex_pub == lc.b64u_encode(DEVICE.kex_pub)
    assert device.sign_pub == lc.b64u_encode(DEVICE.sign_pub)


@pytest.mark.parametrize("answer", ["y", 1, None, "True"])
def test_only_a_literal_yes_confirms(relay, answer):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(None)])
    with pytest.raises(linkpair.PairingRejected):
        _accept(relay, _wait(relay, start), start, lambda n: answer)


# -- small-order device keys ------------------------------------------------------


@pytest.mark.parametrize(
    "point",
    [
        pytest.param("01" + "00" * 31, id="identity"),
        pytest.param("01" + "00" * 30 + "80", id="identity-negative-zero"),
        pytest.param(
            "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
            id="order-8",
        ),
        pytest.param(
            "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
            id="order-8-other-x",
        ),
        pytest.param("ec" + "ff" * 30 + "7f", id="order-2"),
        pytest.param("00" * 32, id="order-4"),
        pytest.param("ed" + "ff" * 30 + "7f", id="non-canonical-p"),
        pytest.param("ee" + "ff" * 30 + "7f", id="non-canonical-p-plus-1"),
    ],
)
def test_a_small_order_sign_pub_is_refused(relay, point):
    start = _start(relay)
    weak = bytes.fromhex(point)
    device = {**_device(None), "signPub": lc.b64u_encode(weak)}
    device["mac"] = lc.pair_mac(start.secret, DEVICE.kex_pub, weak)
    with pytest.raises(RelayError, match="weak signPub"):
        _accept(relay, _claim(relay, device), start, _no_prompt)


@pytest.mark.parametrize(
    "point",
    [
        pytest.param("00" * 32, id="zero"),
        pytest.param("01" + "00" * 31, id="one"),
        pytest.param(
            "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
            id="order-8",
        ),
        pytest.param(
            "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
            id="order-8-other",
        ),
        pytest.param("ec" + "ff" * 30 + "7f", id="p-minus-1"),
        pytest.param("ed" + "ff" * 30 + "7f", id="non-canonical-p"),
        pytest.param("ee" + "ff" * 30 + "7f", id="non-canonical-p-plus-1"),
        pytest.param("00" * 31 + "80", id="zero-top-bit"),
    ],
)
def test_a_small_order_kex_pub_is_refused(relay, point):
    start = _start(relay)
    weak = bytes.fromhex(point)
    device = {**_device(None), "kexPub": lc.b64u_encode(weak)}
    device["mac"] = lc.pair_mac(start.secret, weak, DEVICE.sign_pub)
    with pytest.raises(RelayError, match="weak kexPub"):
        _accept(relay, _claim(relay, device), start, _no_prompt)


# -- the host token: validated, taken once ----------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "mlh_short",
        "mlp_DECOY_not_a_host_token_0000000000000",
        "mlh_DECOY_has a space_0000000000000000",
        "mlh_DECOY_" + chr(0xE9) * 20,
        "mlh_DECOY_" + "0" * 300,
        "",
        42,
    ],
)
def test_a_malformed_host_token_is_refused_without_echoing_it(relay, token):
    start = _start(relay)
    poll = {**_claim(relay, _device(start)), "hostToken": token}
    with pytest.raises(RelayError) as exc:
        _accept(relay, poll, start)
    assert "hostToken" in str(exc.value)
    assert not isinstance(token, str) or not token or token not in str(exc.value)


def test_a_new_host_claim_without_its_token_pairs_nothing(relay):
    start = _start(relay)
    poll = _claim(relay, _device(None))
    del poll["hostToken"]
    with pytest.raises(linkpair.PairingRejected, match="Hosts page") as exc:
        _accept(relay, poll, start, _no_prompt)
    assert "start the pairing again" in str(exc.value)


def test_a_re_poll_after_a_lost_reply_cannot_pair_a_new_host(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(start)])
    first = _wait(relay, start)
    assert first["hostToken"] == relay.host_token
    again = _wait(relay, start)  # the relay hands the token out once
    assert "hostToken" not in again
    with pytest.raises(linkpair.PairingRejected, match="start the pairing again"):
        _accept(relay, again, start)


def test_add_device_keeps_the_existing_token(relay):
    start = _start(relay, host_token=relay.host_token)
    outcome = _accept(relay, _claim(relay, _device(start)), start)
    assert start.adding and outcome.host_token is None


def test_add_device_with_other_keys_is_refused_by_the_relay(monkeypatch):
    other = lc.generate_key_pairs()
    keys = (lc.b64u_encode(other.kex_pub), lc.b64u_encode(other.sign_pub))
    with FakeRelay(host_keys=keys) as relay:
        _pin(monkeypatch, relay.url, relay.url)
        with pytest.raises(RelayError) as exc:
            _start(relay, host_token=relay.host_token)
        assert exc.value.status == 409 and relay.pair_starts == []


# -- a refused new host is released on the relay ----------------------------------


@pytest.mark.parametrize("path", ["wrong-mac", "declined", "weak-key", "bad-mac"])
def test_a_refused_new_host_is_released(relay, path):
    start = _start(relay)
    device = _device(None if path == "declined" else start)
    if path == "wrong-mac":
        device["mac"] = lc.pair_mac(b"x" * 16, DEVICE.kex_pub, DEVICE.sign_pub)
    if path == "weak-key":
        device["signPub"] = lc.b64u_encode(bytes.fromhex("01" + "00" * 31))
    if path == "bad-mac":
        device["mac"] = "not a mac"
    relay.stage_pairing(claimed_devices=[device])
    with pytest.raises((linkpair.PairingRejected, RelayError)):
        _accept(relay, _wait(relay, start), start, lambda n: False)
    assert relay.deleted and _deletes(relay) == [f"Bearer {relay.host_token}"]


def test_a_claim_without_a_host_id_still_releases_the_host(relay):
    start = _start(relay)
    poll = _claim(relay, _device(start))
    del poll["hostId"]
    with pytest.raises(RelayError, match="hostId"):
        _accept(relay, poll, start, _no_prompt)
    assert _deletes(relay) == [f"Bearer {relay.host_token}"]


def test_a_refused_add_device_never_unlinks_the_host(relay):
    start = _start(relay, host_token=relay.host_token)
    relay.stage_pairing(claimed_devices=[_device(None)], new_host=False)
    with pytest.raises(linkpair.PairingRejected):
        _accept(relay, _wait(relay, start), start, lambda n: False)
    assert not relay.deleted and _deletes(relay) == []


def test_a_failed_release_still_refuses(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(None)])
    poll = _wait(relay, start)
    relay.revoke()
    with pytest.raises(linkpair.PairingRejected, match="not confirmed"):
        _accept(relay, poll, start, lambda n: False)
    assert _deletes(relay) == [f"Bearer {relay.host_token}"]


def test_the_release_is_one_attempt_even_when_rate_limited(relay, monkeypatch):
    start = _start(relay)
    poll = _claim(relay, _device(None))
    _limit_first_call(monkeypatch, 30)
    slept: list[float] = []
    client = RelayClient(relay.url, timeout=5, sleep=slept.append)
    with pytest.raises(linkpair.PairingRejected):
        linkpair.accept_claim(poll, start, HOST, confirm=lambda n: False, client=client)
    assert slept == [] and _deletes(relay) == []  # the one DELETE was the 429


def test_no_release_on_success(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(start)])
    _accept(relay, _wait(relay, start), start)
    assert _deletes(relay) == []


def test_an_interrupted_prompt_releases_the_new_host(relay):
    start = _start(relay)
    relay.stage_pairing(claimed_devices=[_device(None)])

    def interrupted(number: str) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _accept(relay, _wait(relay, start), start, interrupted)
    assert _deletes(relay) == [f"Bearer {relay.host_token}"]


# -- the QR fields ------------------------------------------------------------------


def test_the_qr_fields_are_canonical(relay):
    start = _start(relay)
    fragment = linkpair.qr_url(start, HOST).split("#", 1)[1]
    fields = dict(part.split("=", 1) for part in fragment.split("&"))
    assert list(fields) == ["c", "k", "s"]
    assert len(fields["s"]) == 22 and lc.b64u_decode(fields["s"]) == start.secret
    assert len(fields["k"]) == 22 and len(lc.b64u_decode(fields["k"])) == 16


@pytest.mark.parametrize(
    "change",
    [
        {"secret": b"x" * 15},
        {"secret": b"x" * 17},
        {"code": "ABCD0FGH"},
        {"code": "abcdefgh"},
        {"code": "ABCDEFG"},
    ],
)
def test_qr_url_refuses_a_bad_field(relay, change):
    start = dataclasses.replace(_start(relay), **change)
    with pytest.raises(ValueError):
        linkpair.qr_url(start, HOST)


def test_the_default_app_refuses_port_zero(monkeypatch):
    monkeypatch.delenv("MAGENT_LINK_APP_URL", raising=False)
    monkeypatch.setattr(env, "_cached_env", None)
    client = _replying(
        monkeypatch, "https://relay.example", appUrl="https://app.magent.now:0"
    )
    with pytest.raises(RelayError, match="appUrl"):
        _start_with(client)


def test_a_pairing_through_the_shared_fake_relay_fixture(fake_relay):
    start = _start(fake_relay)
    fake_relay.stage_pairing(claimed_devices=[_device(start)])
    outcome = _accept(fake_relay, _wait(fake_relay, start), start, _no_prompt)
    assert outcome.via_qr and outcome.host_token == fake_relay.host_token
