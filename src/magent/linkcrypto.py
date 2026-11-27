"""link-v1 crypto: the one implementation of contract C6 on the host side.

The relay routes ciphertext only. Per (host, device) pair, X25519 ECDH plus
HKDF-SHA256 gives two directional AES-256-GCM keys (h2d, d2h); every sealed
blob is ``b64u(nonce(12) || ciphertext || tag)`` with a use-specific AAD;
commands are Ed25519-signed by the device. Uploads are 1 MiB AES-GCM chunks
under a random per-upload content key, the last chunk flagged in its AAD so a
truncated object never decrypts.

``cryptography`` is the optional ``[link]`` extra, imported inside each
function: ``import magent.linkcrypto`` always works, and a crypto call without
the extra raises LinkCryptoUnavailable naming the install command.

The same vectors pin the relay's and the app's implementations, through
their vendored copies: ``scripts/gen_link_vectors.py`` writes
``tests/vectors/link-v1.json`` from this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )

CHUNK_BYTES = 1048576
_NONCE = 12
_TAG = 16
_KEY = 32
_FRAME = _NONCE + CHUNK_BYTES + _TAG

AAD_CMD = "cmd:"
AAD_RES = "res:"
AAD_EVT = "evt:"
AAD_SNAP = "snap:"
AAD_UKEY = "ukey:"
AAD_UMETA = "umeta:"

_H2D_INFO = b"magent-link/v1/h2d"
_D2H_INFO = b"magent-link/v1/d2h"
_SAFETY_LABEL = b"magent-link/v1/safety"

INSTALL_HINT = 'pip install "magent-multi-ai-agents-manager[link]"'


class LinkCryptoUnavailable(RuntimeError):
    """The ``[link]`` extra (cryptography) is not installed."""


class SealError(ValueError):
    """A sealed blob or upload chunk did not authenticate."""


@dataclass(frozen=True)
class KeyPairs:
    # The private halves stay out of repr(), so a log line or a traceback that
    # formats a KeyPairs never prints a key.
    kex_priv: bytes = field(repr=False)
    kex_pub: bytes
    sign_priv: bytes = field(repr=False)
    sign_pub: bytes


@dataclass(frozen=True)
class PairKeys:
    # Both are secret AES keys: never in repr().
    h2d: bytes = field(repr=False)
    d2h: bytes = field(repr=False)


def _require() -> None:
    """LinkCryptoUnavailable unless the ``cryptography`` package imports."""
    try:
        import cryptography  # noqa: F401  # reason: availability probe; the modules are imported where used
    except ImportError as exc:
        raise LinkCryptoUnavailable(
            f"magent link needs the [link] extra: {INSTALL_HINT}"
        ) from exc


def _require_key(key: bytes) -> None:
    """ValueError unless ``key`` is a 32-byte AES-256 key.

    AESGCM alone would accept 16 and 24 bytes too, silently dropping to
    AES-128 or AES-192; link-v1 is AES-256 everywhere.
    """
    if len(key) != _KEY:
        raise ValueError(f"key must be {_KEY} bytes, got {len(key)}")


def b64u_encode(data: bytes) -> str:
    """Base64url without padding: the only binary encoding on the wire."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    """Inverse of b64u_encode. ValueError on anything that is not base64url.

    Only the canonical form is accepted, as in the relay and magent-app: no
    padding, no ``+`` or ``/``, no impossible length (``len % 4 == 1``), and no
    set bits in the unused tail of the last character. So every byte string
    has exactly one wire spelling.
    """
    if not isinstance(text, str) or any(c in text for c in "+/= \n"):
        raise ValueError("not base64url")
    try:
        raw = base64.b64decode(
            text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc
    if b64u_encode(raw) != text:
        raise ValueError("not base64url")
    return raw


def _raw_private(priv: X25519PrivateKey | Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def _raw_public(key: X25519PublicKey | Ed25519PublicKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def public_from_private(kex_priv: bytes, sign_priv: bytes) -> KeyPairs:
    """The full key pairs for two raw 32-byte private keys."""
    _require()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    kex = X25519PrivateKey.from_private_bytes(kex_priv)
    sig = Ed25519PrivateKey.from_private_bytes(sign_priv)
    return KeyPairs(
        kex_priv=_raw_private(kex),
        kex_pub=_raw_public(kex.public_key()),
        sign_priv=_raw_private(sig),
        sign_pub=_raw_public(sig.public_key()),
    )


def generate_key_pairs() -> KeyPairs:
    """Fresh X25519 (key exchange) and Ed25519 (signing) key pairs."""
    _require()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    return public_from_private(
        _raw_private(X25519PrivateKey.generate()),
        _raw_private(Ed25519PrivateKey.generate()),
    )


def fingerprint(kex_pub: bytes, sign_pub: bytes) -> str:
    """The host fingerprint the QR carries as ``k=``."""
    return b64u_encode(hashlib.sha256(kex_pub + sign_pub).digest()[:16])


def pair_keys(
    own_kex_priv: bytes, peer_kex_pub: bytes, host_id: str, device_id: str
) -> PairKeys:
    """The two directional keys for one (host, device) pair; both sides agree."""
    _require()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    shared = X25519PrivateKey.from_private_bytes(own_kex_priv).exchange(
        X25519PublicKey.from_public_bytes(peer_kex_pub)
    )
    salt = (host_id + device_id).encode("utf-8")

    def derive(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(
            shared
        )

    return PairKeys(h2d=derive(_H2D_INFO), d2h=derive(_D2H_INFO))


def seal(key: bytes, plaintext: bytes, aad: str, *, _nonce: bytes | None = None) -> str:
    """AES-256-GCM: ``b64u(nonce || ciphertext || tag)`` under a fresh random nonce.

    ``_nonce`` exists only for the deterministic test vectors; product code
    never passes it (a repeated nonce under one key breaks AES-GCM).
    """
    _require()
    _require_key(key)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    n = os.urandom(_NONCE) if _nonce is None else _nonce
    if len(n) != _NONCE:
        raise ValueError("nonce must be 12 bytes")
    return b64u_encode(n + AESGCM(key).encrypt(n, plaintext, aad.encode("utf-8")))


def open_sealed(key: bytes, sealed: str, aad: str) -> bytes:
    """The plaintext of a sealed blob. SealError if it does not authenticate."""
    _require()
    _require_key(key)
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = b64u_decode(sealed)
    except ValueError as exc:
        raise SealError("sealed blob is not base64url") from exc
    if len(raw) < _NONCE + _TAG:
        raise SealError("sealed blob is too short")
    try:
        return AESGCM(key).decrypt(raw[:_NONCE], raw[_NONCE:], aad.encode("utf-8"))
    except InvalidTag as exc:
        raise SealError("sealed blob did not authenticate") from exc


def command_signing_input(
    cid: str, host_id: str, method: str, path: str, ts: int, sealed: str
) -> bytes:
    """What the device signs: the routing fields, then sha256 of the sealed bytes.

    The fields are joined with newlines and not escaped, so callers must
    validate ``cid``, ``method``, ``path`` and ``ts`` (no newline, the expected
    shape) before trusting a signature over them.
    """
    head = f"{cid}\n{host_id}\n{method}\n{path}\n{ts}\n".encode()
    return head + hashlib.sha256(b64u_decode(sealed)).digest()


def sign(sign_priv: bytes, message: bytes) -> str:
    """Ed25519 signature, b64u. The host never signs commands; vectors and tests do."""
    _require()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return b64u_encode(Ed25519PrivateKey.from_private_bytes(sign_priv).sign(message))


def verify(sign_pub: bytes, message: bytes, sig: str) -> bool:
    """True only for a valid Ed25519 signature by ``sign_pub``."""
    _require()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        raw = b64u_decode(sig)
        Ed25519PublicKey.from_public_bytes(sign_pub).verify(raw, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def safety_number(
    host_kex_pub: bytes, host_sign_pub: bytes, dev_kex_pub: bytes, dev_sign_pub: bytes
) -> str:
    """The 6-digit number both sides show on the typed-code path: ``"482 913"``."""
    digest = hashlib.sha256(
        _SAFETY_LABEL + host_kex_pub + host_sign_pub + dev_kex_pub + dev_sign_pub
    ).digest()
    text = f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
    return f"{text[:3]} {text[3:]}"


def pair_mac(secret: bytes, dev_kex_pub: bytes, dev_sign_pub: bytes) -> str:
    """The QR path's proof that the claiming device saw the QR secret."""
    return b64u_encode(
        hmac.new(secret, dev_kex_pub + dev_sign_pub, hashlib.sha256).digest()
    )


def _chunk_aad(upload_id: str, index: int, last: bool) -> bytes:
    return f"{upload_id}:{index}:{1 if last else 0}".encode()


def encrypt_upload(
    content_key: bytes,
    upload_id: str,
    plaintext: bytes,
    *,
    _nonces: list[bytes] | None = None,
) -> bytes:
    """The staged object: 1 MiB chunks, each ``nonce || AES-GCM(chunk)``.

    An empty file is one empty final chunk. Every chunk gets a fresh random
    nonce; ``_nonces`` (one per chunk) exists only for the test vectors.
    """
    _require()
    _require_key(content_key)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    count = max(1, -(-len(plaintext) // CHUNK_BYTES))
    if _nonces is not None and len(_nonces) != count:
        raise ValueError(f"need {count} nonces, got {len(_nonces)}")
    aead = AESGCM(content_key)
    out = bytearray()
    for i in range(count):
        chunk = plaintext[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES]
        n = os.urandom(_NONCE) if _nonces is None else _nonces[i]
        out += n + aead.encrypt(n, chunk, _chunk_aad(upload_id, i, i == count - 1))
    return bytes(out)


def iter_decrypt_upload(
    content_key: bytes, upload_id: str, chunks: Iterable[bytes]
) -> Iterator[bytes]:
    """Decrypt a staged object arriving in arbitrary pieces, one plaintext chunk at a time.

    A full frame is only decrypted once more bytes follow it, so the final
    frame is always the one opened with ``last=1``: dropping it, or cutting the
    object on a frame boundary, fails authentication (SealError).

    Each chunk is yielded as soon as it authenticates, BEFORE the rest of the
    object has: a later chunk can still raise SealError after earlier
    plaintext was handed out. Callers must stage what they receive (never act
    on it, never move it into place) until the iterator is exhausted without
    error, and discard the staged bytes on SealError.
    """
    _require()
    _require_key(content_key)
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead = AESGCM(content_key)
    buf = bytearray()
    index = 0

    def open_frame(frame: bytes, last: bool) -> bytes:
        if len(frame) < _NONCE + _TAG:
            raise SealError("upload chunk is too short")
        try:
            return aead.decrypt(
                frame[:_NONCE], frame[_NONCE:], _chunk_aad(upload_id, index, last)
            )
        except InvalidTag as exc:
            raise SealError(f"upload chunk {index} did not authenticate") from exc

    for piece in chunks:
        buf += piece
        while len(buf) > _FRAME:
            yield open_frame(bytes(buf[:_FRAME]), False)
            del buf[:_FRAME]
            index += 1
    yield open_frame(bytes(buf), True)
