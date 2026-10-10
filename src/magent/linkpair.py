"""Pairing this host with an app device over the relay (spec 4.1 "Pairing").

``start_pairing`` registers the host's public keys and gets an 8-character
code; ``qr_url`` is what the QR encodes (its fragment, which carries the
16-byte secret, never reaches the relay); ``wait_for_claim`` long-polls until
a signed-in app claims the code, or the code expires (``deadline_of``);
``accept_claim`` decides whether to trust the claiming device:

* QR path -- the device proves it saw the QR: ``mac = HMAC(secret, device
  keys)``. Verified here; no prompt.
* typed-code path -- no mac. Both sides show the 6-digit safety number derived
  from both key sets; ``confirm(number)`` (the CLI's y/N prompt) decides.

Anything else pairs nothing (PairingRejected); a new host that pairs nothing
is released on the relay again. No printing, no prompting, no files: the CLI
shell owns all three, and its safety-number prompt defaults to No.

Known limit of the typed path: the relay sees both key sets before it
forwards them, so it could grind its own keys until the 6-digit number
matches. Closing that takes a commitment, not a local check: each side
commits to a nonce before it sees the other side's keys, and the nonces are
folded into the safety number. That changes pair/start, the claim, the app
and ``linkcrypto.safety_number``'s inputs; here it would touch
``start_pairing`` (send the commitment), ``_read_device`` (check the reveal)
and ``_confirm_safety_number`` (the number's inputs), which is why reading
the claim and judging it stay separate steps.
"""

from __future__ import annotations

import hmac
import math
import re
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from magent import linkcrypto
from magent.link import RATE_LIMIT_CAP_S, Backoff, RelayError
from magent.link import app_url as configured_app_url
from magent.linkfiles import TrustedDevice
from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from urllib.parse import SplitResult

    from magent.link import RelayClient
    from magent.linkcrypto import KeyPairs

POLL_TIMEOUT_S = 35.0  # the relay holds a poll 25 s; leave room for the tunnel
PENDING_PAUSE_S = 1.0
CODE_ALPHABET = frozenset("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")  # contract C7
CODE_LENGTH = 8
SECRET_BYTES = 16  # the QR's s=: 22 b64u characters
FINGERPRINT_CHARS = 22  # the QR's k=: b64u of 16 bytes of sha256
MAC_CHARS = 43  # b64u of a 32-byte HMAC-SHA256
# Refused in a device name: control, format, surrogate and private-use code
# points. Unassigned ones (Cn) are allowed, so a phone named with an emoji
# newer than this Python's Unicode tables still pairs.
NAME_REFUSED_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Co"))
NAME_MAX = 100
# Contract C7: "mlh_" + b64u(32 bytes). The bound is looser than 43 so a relay
# that lengthens its tokens still pairs; anything outside it is refused before
# it can reach link.json.
HOST_TOKEN_RE = re.compile(r"mlh_[A-Za-z0-9_-]{16,256}")
# Small-order public keys, as blocklists because pyca/cryptography does no
# point math. Both are libsodium's lists, compared with the top bit masked:
# the y-coordinates (Ed25519) or u-coordinates (X25519) of all eight 8-torsion
# points (1, p-1, 0 and the two order-8 values) plus the non-canonical p and
# p + 1. Masking the top bit covers both x of each Ed25519 point, its
# "negative zero" spellings, and the bit X25519 ignores. An Ed25519 key of
# small order verifies any message under OpenSSL; an X25519 one makes every
# shared secret zero.
_SMALL_ORDER_Y = frozenset(
    bytes.fromhex(h)
    for h in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
        "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    )
)
_SMALL_ORDER_U = frozenset(
    bytes.fromhex(h)
    for h in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
        "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
        "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
    )
)


class PairingRejected(Exception):
    """The claiming device could not be verified; nothing was paired."""


class PairingExpired(Exception):
    """The code expired (or the wait ran out) before a device claimed it."""


class PairingRateLimited(Exception):
    """The relay refused pair/start for now (429). Nothing was started.

    ``retry_after`` is the relay's Retry-After in seconds, else None; the
    message is ready for the user ("try again in N min").
    """

    def __init__(self, retry_after: float | None) -> None:
        when = (
            "in a few minutes"
            if retry_after is None
            else f"in {max(1, math.ceil(retry_after / 60))} min"
        )
        super().__init__(f"the relay is limiting new pairings; try again {when}")
        self.retry_after = retry_after


@dataclass(frozen=True)
class PairingStart:
    pairing_id: str
    code: str  # undashed, as the QR and display_code want it
    # The poll token and the QR secret stay out of repr(), so a log line or a
    # traceback that formats a PairingStart never prints either.
    poll_token: str = field(repr=False)
    expires_at: str
    app_url: str
    secret: bytes = field(repr=False)
    # True when start_pairing ran with the host's token (add a device): the
    # claim then carries no new token, and the existing one stays in force.
    adding: bool = False


@dataclass(frozen=True)
class ClaimOutcome:
    host_id: str
    host_token: str | None = field(repr=False)
    devices: tuple[TrustedDevice, ...]
    via_qr: bool


@dataclass(frozen=True)
class _Claimed:
    """The claiming device as the relay forwarded it, not yet trusted. Every
    field is immutable and taken once, so what is verified is what is kept."""

    device_id: str
    name: str
    kex_pub: bytes
    sign_pub: bytes
    mac: str | None


def _bad(what: str) -> RelayError:
    return RelayError(0, "invalid_response", what)


def _text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise _bad(f"pairing reply lacks {key}")
    return value


def _key(data: dict[str, object], key: str) -> bytes:
    try:
        raw = linkcrypto.b64u_decode(_text(data, key))
    except ValueError as exc:
        raise _bad(f"pairing reply has a bad {key}") from exc
    if len(raw) != 32:
        raise _bad(f"pairing reply has a bad {key}")
    return raw


def _expiry(expires_at: str) -> float:
    """``expiresAt`` (ISO 8601, UTC) as a time.time() value."""
    text = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _bad("pairing reply has a bad expiresAt") from exc
    if moment.tzinfo is None:
        raise _bad("pairing reply has a bad expiresAt")
    return moment.timestamp()


def _normalize_code(raw: str) -> str:
    """The code as the relay normalizes it: no dash or whitespace, upper case,
    then exactly 8 characters of the C7 alphabet. The relay sends it dashed."""
    code = "".join(ch for ch in raw if ch != "-" and not ch.isspace()).upper()
    if len(code) != CODE_LENGTH or not set(code) <= CODE_ALPHABET:
        raise _bad("pairing reply has a bad code")
    return code


def _origin(parts: SplitResult) -> tuple[str, str, int | None]:
    default = {"https": 443, "http": 80}.get(parts.scheme)
    port = parts.port if parts.port is not None else default  # ":0" is no default
    return parts.scheme, parts.hostname or "", port


def _check_app_url(app_url: str, relay_base: str) -> None:
    """RelayError unless ``appUrl`` is the configured app (MAGENT_LINK_APP_URL)
    by origin -- scheme, host and port -- over https (http only when the relay
    itself is http, i.e. a local dev or test relay), with no userinfo, query
    or fragment. The QR carries the pairing secret to that page, so a relay
    must not be able to aim it anywhere else."""
    refused = _bad("pairing reply has an appUrl that is not this host's app")
    odd = (chr(92), "@", "?", "#")
    if not app_url.isascii() or any(c <= " " or c in odd for c in app_url):
        raise refused
    try:
        got, want = urlsplit(app_url), urlsplit(configured_app_url())
        got_origin, want_origin = _origin(got), _origin(want)
    except ValueError as exc:
        raise refused from exc
    secure_only = urlsplit(relay_base).scheme != "http"
    if got.scheme != "https" and (secure_only or got.scheme != "http"):
        raise refused
    if got.username is not None or got.password is not None:
        raise refused
    if not got.hostname or got_origin != want_origin:
        raise refused


def display_code(code: str) -> str:
    """``ABCDEFGH`` -> ``ABCD-EFGH`` (how the code is shown and typed)."""
    return f"{code[:4]}-{code[4:]}"


def start_pairing(
    client: RelayClient,
    keys: KeyPairs,
    *,
    name: str,
    platform: str,
    version: str,
    host_token: str | None = None,
) -> PairingStart:
    """POST /v1/host/pair/start. ``host_token`` = add a device to this host.

    A 429 is never waited out here (the relay's limit on new pairings can
    run to an hour): it is PairingRateLimited at once, so the user is told
    when to come back instead of watching a hang.
    """
    try:
        data = client.call(
            "POST",
            "/v1/host/pair/start",
            {
                "hostKexPub": linkcrypto.b64u_encode(keys.kex_pub),
                "hostSignPub": linkcrypto.b64u_encode(keys.sign_pub),
                "name": name,
                "platform": platform,
                "daemonVersion": version,
            },
            token=host_token,
            retries=0,
        )
    except RelayError as exc:
        if exc.status == 429:
            raise PairingRateLimited(exc.retry_after) from exc
        raise
    code = _normalize_code(_text(data, "code"))
    app_url = _text(data, "appUrl")
    _check_app_url(app_url, client.base_url)
    expires_at = _text(data, "expiresAt")
    _expiry(expires_at)
    return PairingStart(
        pairing_id=_text(data, "pairingId"),
        code=code,
        poll_token=_text(data, "pollToken"),
        expires_at=expires_at,
        app_url=app_url,
        secret=secrets.token_bytes(SECRET_BYTES),
        adding=host_token is not None,
    )


def deadline_of(start: PairingStart) -> float:
    """When the code expires, as the ``deadline`` for ``wait_for_claim``."""
    return _expiry(start.expires_at)


def qr_url(start: PairingStart, keys: KeyPairs) -> str:
    """What the QR encodes. The fragment never reaches the relay.

    ``s`` is the canonical b64u of exactly 16 bytes and ``k`` the canonical
    22-character fingerprint; anything else is a bug here, not a QR to show.
    """
    if len(start.secret) != SECRET_BYTES:
        raise ValueError("the QR secret must be 16 bytes")
    if len(start.code) != CODE_LENGTH or not set(start.code) <= CODE_ALPHABET:
        raise ValueError("not a pairing code")
    fp = linkcrypto.fingerprint(keys.kex_pub, keys.sign_pub)
    if len(fp) != FINGERPRINT_CHARS or len(linkcrypto.b64u_decode(fp)) != 16:
        raise ValueError("not a host fingerprint")
    secret = linkcrypto.b64u_encode(start.secret)
    return f"{start.app_url.rstrip('/')}/pair#c={start.code}&k={fp}&s={secret}"


def wait_for_claim(
    client: RelayClient,
    start: PairingStart,
    *,
    deadline: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Long-poll until the code is claimed; the claimed poll reply.

    Every wait here is this loop's, against ``deadline``: the client never
    retries a 429 itself. A 429 (the relay does not limit polls, a proxy in
    front of it might) does not end the wait while the code is live: the poll
    resumes after its Retry-After (else a jittered backoff), and no pause runs
    past ``deadline``.
    """
    path = f"/v1/host/pair/poll?pairingId={quote(start.pairing_id, safe='')}"
    backoff = Backoff(cap=RATE_LIMIT_CAP_S)

    def pause(seconds: float) -> None:
        sleep(max(0.0, min(seconds, RATE_LIMIT_CAP_S, deadline - clock())))

    while clock() < deadline:
        try:
            data = client.call(
                "GET", path, token=start.poll_token, timeout=POLL_TIMEOUT_S, retries=0
            )
        except RelayError as exc:
            if exc.status != 429:
                raise
            pause(backoff.next_delay() if exc.retry_after is None else exc.retry_after)
            continue
        backoff.reset()
        status = data.get("status")
        if status == "claimed":
            return data
        if status == "expired":
            raise PairingExpired("the pairing code expired")
        if status != "pending":
            raise _bad("unknown pairing status")
        pause(PENDING_PAUSE_S)
    raise PairingExpired("no device claimed the code in time")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_token(poll: dict[str, object]) -> str | None:
    """The new host token (None when the claim has none)."""
    token = poll.get("hostToken")
    if token is None:
        return None
    if not isinstance(token, str) or not HOST_TOKEN_RE.fullmatch(token):
        # Never echo it: a near-miss token is still a credential.
        raise _bad("pairing reply has a malformed hostToken")
    return token


def _device_id(item: dict[str, object]) -> str:
    value = _text(item, "deviceId")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise _bad("the claim has a bad deviceId") from exc
    if canonical != value:
        raise _bad("the claim has a bad deviceId")
    return value


def _device_name(item: dict[str, object]) -> str:
    """1-100 characters, not blank, and no control, format, surrogate or
    private-use character: the name reaches the terminal and link.json."""
    value = item.get("name")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= NAME_MAX
        or not value.strip()
        or any(unicodedata.category(c) in NAME_REFUSED_CATEGORIES for c in value)
    ):
        raise _bad("the claim has a bad device name")
    return value


def _mac(item: dict[str, object]) -> str | None:
    """None on the typed-code path; else a canonical 43-character b64u mac.
    A mac that is there but malformed is refused, never read as "no mac"."""
    mac = item.get("mac")
    if mac is None:
        return None
    if not isinstance(mac, str) or len(mac) != MAC_CHARS:
        raise _bad("the claim has a bad mac")
    try:
        raw = linkcrypto.b64u_decode(mac)
    except ValueError as exc:
        raise _bad("the claim has a bad mac") from exc
    if len(raw) != 32:
        raise _bad("the claim has a bad mac")
    return mac


def _masked(pub: bytes) -> bytes:
    return pub[:31] + bytes([pub[31] & 0x7F])


def _read_device(poll: dict[str, object]) -> _Claimed:
    """The one claiming device, checked for shape before anything is verified
    or anyone is asked. A pairing claims exactly one device, so more (or
    none) is a malformed claim. The keys decoded here are the ones verified
    and then trusted: nothing re-reads the reply."""
    raw_devices = poll.get("devices")
    if not isinstance(raw_devices, list) or len(raw_devices) != 1:
        raise _bad("the claim must name exactly one device")
    raw = raw_devices[0]
    if not isinstance(raw, dict):
        raise _bad("the claim has a bad device")
    item = {str(k): v for k, v in raw.items()}
    kex_pub = _key(item, "kexPub")
    if _masked(kex_pub) in _SMALL_ORDER_U:
        raise _bad("the claim has a weak kexPub")
    sign_pub = _key(item, "signPub")
    if _masked(sign_pub) in _SMALL_ORDER_Y:
        raise _bad("the claim has a weak signPub")
    return _Claimed(
        device_id=_device_id(item),
        name=_device_name(item),
        kex_pub=kex_pub,
        sign_pub=sign_pub,
        mac=_mac(item),
    )


def _check_token(start: PairingStart, token: str | None) -> None:
    """PairingRejected when a new host's claim came without its token."""
    if not start.adding and token is None:
        raise PairingRejected(
            "the relay sent no host token (it is sent only once, and that reply"
            " was lost), so nothing was paired; remove this host on the Hosts"
            " page of the app first (it still holds a host slot), then start the"
            " pairing again"
        )


def _check_qr_proof(device: _Claimed, start: PairingStart) -> None:
    """PairingRejected unless the device's mac is HMAC(secret, its keys).

    A wrong mac is final: it never falls back to the safety number.
    """
    expected = linkcrypto.pair_mac(start.secret, device.kex_pub, device.sign_pub)
    if not hmac.compare_digest(
        (device.mac or "").encode("ascii"), expected.encode("ascii")
    ):
        raise PairingRejected(
            "the device's QR proof did not verify; nothing was paired"
        )


def _confirm_safety_number(
    device: _Claimed, keys: KeyPairs, confirm: Callable[[str], bool]
) -> None:
    """PairingRejected unless the user confirms the safety number matches.

    Only a literal True confirms; the CLI's prompt defaults to No.
    """
    number = linkcrypto.safety_number(
        keys.kex_pub, keys.sign_pub, device.kex_pub, device.sign_pub
    )
    if confirm(number) is not True:
        raise PairingRejected(
            "the safety numbers were not confirmed; nothing was paired"
        )


def _release_host(client: RelayClient, token: str) -> None:
    """DELETE /v1/host for a new host that paired nothing, so the relay frees
    its row and the account's host slot. Best effort, one attempt: a failure
    is logged (without the token) and the rejection still stands."""
    try:
        client.call("DELETE", "/v1/host", token=token, retries=0)
    except RelayError as exc:
        get_logger("link").warning(
            "link: could not release the unpaired host on the relay (%s %s)",
            exc.status,
            exc.code,
        )


def accept_claim(
    poll: dict[str, object],
    start: PairingStart,
    keys: KeyPairs,
    *,
    confirm: Callable[[str], bool],
    client: RelayClient,
) -> ClaimOutcome:
    """Verify the claiming device (QR mac, or a confirmed safety number).

    The relay hands a new host its token exactly once, in the first claimed
    poll reply. A new-host claim without one (that reply was lost, and a
    re-poll does not repeat it) cannot be saved, so it pairs nothing. When a
    new-host claim that did carry its token is refused for any reason -- a
    malformed claim, a wrong mac, a declined number, an interrupted prompt --
    the host is released through ``client`` (DELETE /v1/host with that
    token). An add-device claim is never released: that would unlink the
    host itself.
    """
    token = None if start.adding else _read_token(poll)
    try:
        host_id = _text(poll, "hostId")
        device = _read_device(poll)
        _check_token(start, token)
        if device.mac is not None:
            _check_qr_proof(device, start)
        else:
            _confirm_safety_number(device, keys, confirm)
    except BaseException:
        # Whatever stopped it, nothing was paired, so the new host's slot is
        # given back.
        if token is not None:
            _release_host(client, token)
        raise
    return ClaimOutcome(
        host_id=host_id,
        host_token=token,
        devices=(
            TrustedDevice(
                device_id=device.device_id,
                name=device.name,
                kex_pub=linkcrypto.b64u_encode(device.kex_pub),
                sign_pub=linkcrypto.b64u_encode(device.sign_pub),
                paired_at=_stamp(),
            ),
        ),
        via_qr=device.mac is not None,
    )
