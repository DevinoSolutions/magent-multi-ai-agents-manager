#!/usr/bin/env python
"""Write tests/vectors/link-v1.json: the link-v1 cross-language test vectors.

    uv run python scripts/gen_link_vectors.py           # rewrite the file
    uv run python scripts/gen_link_vectors.py --check   # exit 1 if it would change

Deterministic: every key, nonce and secret is derived from sha256 of a fixed,
public DECOY label (SEED below), so the output is byte-identical on every
machine, and tests/unit/test_linkcrypto.py proves the committed file is
exactly what this writes. The relay's and the app's vendored copies are
byte-identical to it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from magent import linkcrypto as lc

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "vectors" / "link-v1.json"

HOST_ID = "hst_vector_1"
DEVICE_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8b"
COMMAND_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8c"
UPLOAD_SMALL = "00000000-0000-7000-8000-00000000000d"
UPLOAD_EMPTY = "00000000-0000-7000-8000-00000000000e"
UPLOAD_TWO = "00000000-0000-7000-8000-00000000000f"
PATH = "/api/v1/sessions/marka/pane?lines=200"
TS = 1791500000000


# Every vector key, nonce and secret is sha256 of SEED + a name: public, fixed,
# and marked DECOY so no reader (or secret scanner) mistakes one for a real key.
SEED = "magent-link/v1/vector/DECOY/"


def _label(name: str, n: int = 32) -> bytes:
    return hashlib.sha256(f"{SEED}{name}".encode()).digest()[:n]


def _flat(name: str, n: int = 32) -> bytes:
    """One byte of the label, repeated: for values gitleaks reads as credentials.

    gitleaks' generic-api-key rule flags a high-entropy value written after a
    ``secret`` or ``*_key`` name (or after the ``ukey:`` AAD prefix, hence the
    flat upload ids above), and the relay and magent-app copies of this file
    live outside any allowlisted path. A repeated byte stays under the rule's
    entropy floor. Any bytes prove the implementations agree.
    """
    return _label(name)[:1] * n


def _pattern(n: int) -> bytes:
    return bytes(i % 251 for i in range(n))


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _party(name: str, ident: str) -> tuple[lc.KeyPairs, dict[str, str]]:
    keys = lc.public_from_private(_label(f"{name}/kex"), _label(f"{name}/sign"))
    return keys, {
        "id": ident,
        "kex_priv": lc.b64u_encode(keys.kex_priv),
        "kex_pub": lc.b64u_encode(keys.kex_pub),
        "sign_priv": lc.b64u_encode(keys.sign_priv),
        "sign_pub": lc.b64u_encode(keys.sign_pub),
    }


def _seal(
    name: str, key_name: str, keys: lc.PairKeys, aad: str, plaintext: bytes
) -> dict[str, str]:
    nonce = _label(f"seal/{name}/nonce", 12)
    key = keys.h2d if key_name == "h2d" else keys.d2h
    return {
        "name": name,
        "key": key_name,
        "aad": aad,
        "nonce": lc.b64u_encode(nonce),
        "plaintext": lc.b64u_encode(plaintext),
        "sealed": lc.seal(key, plaintext, aad, _nonce=nonce),
    }


def _upload(name: str, upload_id: str, length: int) -> dict[str, object]:
    key = _flat(f"upload/{name}/key")
    count = max(1, -(-length // lc.CHUNK_BYTES))
    nonces = [_label(f"upload/{name}/nonce/{i}", 12) for i in range(count)]
    blob = lc.encrypt_upload(key, upload_id, _pattern(length), _nonces=nonces)
    entry: dict[str, object] = {
        "name": name,
        "upload_id": upload_id,
        "content_key": lc.b64u_encode(key),
        "plaintext_length": length,
        "nonces": [lc.b64u_encode(n) for n in nonces],
        "ciphertext_length": len(blob),
        "ciphertext_sha256": hashlib.sha256(blob).hexdigest(),
    }
    if len(blob) <= 4096:
        entry["ciphertext"] = lc.b64u_encode(blob)
    return entry


def build_vectors() -> dict[str, object]:
    host_keys, host = _party("host", HOST_ID)
    dev_keys, device = _party("device", DEVICE_ID)
    pk = lc.pair_keys(host_keys.kex_priv, dev_keys.kex_pub, HOST_ID, DEVICE_ID)
    secret = _flat("pair/secret", 16)
    command = _json({"body": None, "method": "GET", "path": PATH})
    result = _json({"body": {"data": {"text": "hello"}, "ok": True}, "status": 200})
    event = _json(
        {
            "data": {"hook_state": "needs-input", "session": "marka"},
            "id": "ab12cd34:7",
            "ts": 1791500000.5,
            "type": "session.state",
        }
    )
    snapshot = _json(
        {"sessions": [{"live": True, "name": "marka", "session": "marka"}]}
    )
    meta = _json({"name": "shot.png", "size": 37, "type": "image/png"})
    seals = [
        _seal("command", "d2h", pk, lc.AAD_CMD + COMMAND_ID, command),
        _seal("result", "h2d", pk, lc.AAD_RES + COMMAND_ID, result),
        _seal("event", "h2d", pk, f"{lc.AAD_EVT}{HOST_ID}:ab12cd34:7", event),
        _seal("snapshot", "h2d", pk, lc.AAD_SNAP + HOST_ID, snapshot),
        _seal(
            "upload_key",
            "d2h",
            pk,
            lc.AAD_UKEY + UPLOAD_SMALL,
            _flat("upload/small/key"),
        ),
        _seal("upload_meta", "d2h", pk, lc.AAD_UMETA + UPLOAD_SMALL, meta),
    ]
    sealed_command = seals[0]["sealed"]
    signing_input = lc.command_signing_input(
        COMMAND_ID, HOST_ID, "GET", PATH, TS, sealed_command
    )
    return {
        "version": "link-v1",
        "plaintext_pattern": "byte i = i % 251",
        "host": host,
        "device": device,
        "fingerprint": lc.fingerprint(host_keys.kex_pub, host_keys.sign_pub),
        "pair_keys": {"h2d": lc.b64u_encode(pk.h2d), "d2h": lc.b64u_encode(pk.d2h)},
        "safety_number": lc.safety_number(
            host_keys.kex_pub, host_keys.sign_pub, dev_keys.kex_pub, dev_keys.sign_pub
        ),
        "pair_mac": {
            "secret": lc.b64u_encode(secret),
            "mac": lc.pair_mac(secret, dev_keys.kex_pub, dev_keys.sign_pub),
        },
        "seals": seals,
        "command_signature": {
            "id": COMMAND_ID,
            "host_id": HOST_ID,
            "method": "GET",
            "path": PATH,
            "ts": TS,
            "sealed": sealed_command,
            "input": lc.b64u_encode(signing_input),
            "sig": lc.sign(dev_keys.sign_priv, signing_input),
        },
        "uploads": [
            _upload("small", UPLOAD_SMALL, 37),
            _upload("empty", UPLOAD_EMPTY, 0),
            _upload("two_chunks", UPLOAD_TWO, lc.CHUNK_BYTES + 5),
        ],
    }


def render(vectors: dict[str, object]) -> str:
    """The file text: 2-space JSON, insertion order, trailing newline."""
    return json.dumps(vectors, indent=2) + "\n"


def main(argv: list[str]) -> int:
    text = render(build_vectors())
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"{OUT} is stale; run scripts/gen_link_vectors.py", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
