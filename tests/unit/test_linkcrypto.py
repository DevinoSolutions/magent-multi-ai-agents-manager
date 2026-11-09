"""link-v1 crypto (contract C6) and its cross-language vectors."""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from magent import linkcrypto as lc

_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _ROOT / "tests" / "vectors" / "link-v1.json"
_GEN = _ROOT / "scripts" / "gen_link_vectors.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_link_vectors", _GEN)
    assert spec is not None and spec.loader is not None  # a loadable script guard
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vectors() -> dict:
    return json.loads(_VECTORS.read_text(encoding="utf-8"))


def _pattern(n: int) -> bytes:
    return bytes(i % 251 for i in range(n))


class TestB64u:
    def test_round_trip_without_padding(self):
        for n in range(40):
            data = _pattern(n)
            text = lc.b64u_encode(data)
            assert "=" not in text and "+" not in text and "/" not in text
            assert lc.b64u_decode(text) == data

    def test_garbage_is_refused(self):
        with pytest.raises(ValueError):
            lc.b64u_decode("not*base64")

    def test_known_values(self):
        assert lc.b64u_decode("aGVsbG8") == b"hello"
        assert lc.b64u_decode("aGVsbA") == b"hell"
        assert lc.b64u_decode("") == b""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("aGVsbG7", id="unused-bits-set"),
            pytest.param("aGVsbB", id="unused-bits-set-short"),
            pytest.param("A" * 42 + "B", id="unused-bits-set-32-bytes"),
            pytest.param("aGVsbG8=", id="padding"),
            pytest.param("a+b/", id="standard-alphabet"),
            pytest.param("abcde", id="impossible-length"),
        ],
    )
    def test_non_canonical_input_is_refused(self, text):
        with pytest.raises(ValueError, match="not base64url"):
            lc.b64u_decode(text)


class TestPrimitives:
    def test_both_sides_derive_the_same_pair_keys(self):
        host = lc.generate_key_pairs()
        dev = lc.generate_key_pairs()
        a = lc.pair_keys(host.kex_priv, dev.kex_pub, "hst_1", "dev_1")
        b = lc.pair_keys(dev.kex_priv, host.kex_pub, "hst_1", "dev_1")
        assert a == b
        assert a.h2d != a.d2h and len(a.h2d) == 32

    def test_public_from_private_recovers_the_public_halves(self):
        pairs = lc.generate_key_pairs()
        again = lc.public_from_private(pairs.kex_priv, pairs.sign_priv)
        assert again == pairs

    def test_seal_round_trips_and_binds_the_aad(self):
        key = bytes(range(32))
        sealed = lc.seal(key, b"pane text", "evt:hst_1:abc:1")
        assert lc.open_sealed(key, sealed, "evt:hst_1:abc:1") == b"pane text"
        with pytest.raises(lc.SealError):
            lc.open_sealed(key, sealed, "evt:hst_1:abc:2")

    def test_every_seal_draws_a_fresh_nonce(self):
        key = bytes(range(32))
        a = lc.b64u_decode(lc.seal(key, b"same input", "aad"))
        b = lc.b64u_decode(lc.seal(key, b"same input", "aad"))
        assert a[:12] != b[:12]
        assert a != b

    def test_another_key_is_refused(self):
        sealed = lc.seal(bytes(range(32)), b"pane text", "a")
        with pytest.raises(lc.SealError):
            lc.open_sealed(bytes(32), sealed, "a")

    def test_repr_never_shows_a_private_or_shared_key(self):
        pairs = lc.generate_key_pairs()
        peer = lc.generate_key_pairs()
        pk = lc.pair_keys(pairs.kex_priv, peer.kex_pub, "hst_1", "dev_1")
        text = repr(pairs) + repr(pk)
        for secret in (pairs.kex_priv, pairs.sign_priv, pk.h2d, pk.d2h):
            for spelling in (lc.b64u_encode(secret), secret.hex(), repr(secret)):
                assert spelling not in text
        # The public halves are not secret and still show.
        assert repr(pairs.kex_pub) in text and repr(pairs.sign_pub) in text

    def test_a_flipped_byte_is_refused(self):
        key = bytes(range(32))
        raw = bytearray(lc.b64u_decode(lc.seal(key, b"x" * 10, "a")))
        raw[15] ^= 1
        with pytest.raises(lc.SealError):
            lc.open_sealed(key, lc.b64u_encode(bytes(raw)), "a")

    def test_a_short_blob_is_refused(self):
        with pytest.raises(lc.SealError):
            lc.open_sealed(bytes(32), lc.b64u_encode(b"short"), "a")

    def test_signatures_verify_and_reject_another_message(self):
        dev = lc.generate_key_pairs()
        msg = lc.command_signing_input(
            "c1", "h1", "GET", "/api/v1/meta", 1, lc.seal(bytes(32), b"{}", "cmd:c1")
        )
        sig = lc.sign(dev.sign_priv, msg)
        assert lc.verify(dev.sign_pub, msg, sig)
        assert not lc.verify(dev.sign_pub, msg + b"x", sig)
        assert not lc.verify(dev.sign_pub, msg, "not-a-signature")

    def test_safety_number_shape(self):
        a, b = lc.generate_key_pairs(), lc.generate_key_pairs()
        number = lc.safety_number(a.kex_pub, a.sign_pub, b.kex_pub, b.sign_pub)
        assert (
            len(number) == 7 and number[3] == " " and number.replace(" ", "").isdigit()
        )


class TestUploads:
    @pytest.mark.parametrize(
        "size", [0, 1, 37, lc.CHUNK_BYTES - 1, lc.CHUNK_BYTES, lc.CHUNK_BYTES + 5]
    )
    def test_round_trip_in_arbitrary_pieces(self, size):
        key = bytes(range(32))
        plain = _pattern(size)
        blob = lc.encrypt_upload(key, "u1", plain)
        pieces = [blob[i : i + 70_001] for i in range(0, len(blob), 70_001)] or [b""]
        assert b"".join(lc.iter_decrypt_upload(key, "u1", pieces)) == plain

    def test_a_dropped_final_chunk_is_refused(self):
        key = bytes(range(32))
        blob = lc.encrypt_upload(key, "u1", _pattern(lc.CHUNK_BYTES + 5))
        first_frame = blob[: 12 + lc.CHUNK_BYTES + 16]
        with pytest.raises(lc.SealError):
            b"".join(lc.iter_decrypt_upload(key, "u1", [first_frame]))

    def test_every_chunk_draws_its_own_nonce(self):
        key = bytes(range(32))
        blob = lc.encrypt_upload(key, "u1", _pattern(lc.CHUNK_BYTES + 5))
        frame = 12 + lc.CHUNK_BYTES + 16
        assert blob[:12] != blob[frame : frame + 12]

    def test_the_same_file_encrypts_differently_each_time(self):
        key = bytes(range(32))
        a = lc.encrypt_upload(key, "u1", _pattern(37))
        b = lc.encrypt_upload(key, "u1", _pattern(37))
        assert a[:12] != b[:12]
        assert a != b

    def test_swapped_chunks_are_refused(self):
        key = bytes(range(32))
        blob = lc.encrypt_upload(key, "u1", _pattern(2 * lc.CHUNK_BYTES + 5))
        frame = 12 + lc.CHUNK_BYTES + 16
        swapped = blob[frame : 2 * frame] + blob[:frame] + blob[2 * frame :]
        assert len(swapped) == len(blob)
        with pytest.raises(lc.SealError):
            b"".join(lc.iter_decrypt_upload(key, "u1", [swapped]))

    def test_the_wrong_key_is_refused(self):
        blob = lc.encrypt_upload(bytes(range(32)), "u1", b"abc")
        with pytest.raises(lc.SealError):
            b"".join(lc.iter_decrypt_upload(bytes(32), "u1", [blob]))

    def test_another_upload_id_is_refused(self):
        key = bytes(range(32))
        blob = lc.encrypt_upload(key, "u1", b"abc")
        with pytest.raises(lc.SealError):
            b"".join(lc.iter_decrypt_upload(key, "u2", [blob]))


class TestVectors:
    def test_the_committed_file_is_exactly_what_the_generator_writes(self):
        gen = _load_gen()
        assert _VECTORS.read_text(encoding="utf-8") == gen.render(gen.build_vectors())

    def test_keys_fingerprint_pair_keys_and_safety_number(self):
        v = _vectors()
        host, dev = v["host"], v["device"]
        hk = lc.public_from_private(
            lc.b64u_decode(host["kex_priv"]), lc.b64u_decode(host["sign_priv"])
        )
        dk = lc.public_from_private(
            lc.b64u_decode(dev["kex_priv"]), lc.b64u_decode(dev["sign_priv"])
        )
        assert lc.b64u_encode(hk.kex_pub) == host["kex_pub"]
        assert lc.b64u_encode(dk.sign_pub) == dev["sign_pub"]
        assert lc.fingerprint(hk.kex_pub, hk.sign_pub) == v["fingerprint"]
        pk = lc.pair_keys(hk.kex_priv, dk.kex_pub, host["id"], dev["id"])
        assert lc.b64u_encode(pk.h2d) == v["pair_keys"]["h2d"]
        assert lc.b64u_encode(pk.d2h) == v["pair_keys"]["d2h"]
        assert (
            lc.safety_number(hk.kex_pub, hk.sign_pub, dk.kex_pub, dk.sign_pub)
            == v["safety_number"]
        )
        mac = lc.pair_mac(
            lc.b64u_decode(v["pair_mac"]["secret"]), dk.kex_pub, dk.sign_pub
        )
        assert mac == v["pair_mac"]["mac"]

    def test_every_seal_opens_and_reseals_identically(self):
        v = _vectors()
        keys = {name: lc.b64u_decode(value) for name, value in v["pair_keys"].items()}
        assert [s["name"] for s in v["seals"]] == [
            "command",
            "result",
            "event",
            "snapshot",
            "upload_key",
            "upload_meta",
        ]
        for s in v["seals"]:
            key = keys[s["key"]]
            plain = lc.b64u_decode(s["plaintext"])
            assert lc.open_sealed(key, s["sealed"], s["aad"]) == plain
            nonce = lc.b64u_decode(s["nonce"])
            assert lc.seal(key, plain, s["aad"], _nonce=nonce) == s["sealed"]

    def test_the_command_signature(self):
        v = _vectors()
        c = v["command_signature"]
        msg = lc.command_signing_input(
            c["id"], c["host_id"], c["method"], c["path"], c["ts"], c["sealed"]
        )
        assert lc.b64u_encode(msg) == c["input"]
        dev_sign_pub = lc.b64u_decode(v["device"]["sign_pub"])
        assert lc.verify(dev_sign_pub, msg, c["sig"])
        assert lc.sign(lc.b64u_decode(v["device"]["sign_priv"]), msg) == c["sig"]

    def test_every_upload_vector(self):
        v = _vectors()
        assert [u["name"] for u in v["uploads"]] == ["small", "empty", "two_chunks"]
        for u in v["uploads"]:
            key = lc.b64u_decode(u["content_key"])
            plain = _pattern(u["plaintext_length"])
            nonces = [lc.b64u_decode(n) for n in u["nonces"]]
            blob = lc.encrypt_upload(key, u["upload_id"], plain, _nonces=nonces)
            assert len(blob) == u["ciphertext_length"]
            assert hashlib.sha256(blob).hexdigest() == u["ciphertext_sha256"]
            if "ciphertext" in u:
                assert lc.b64u_encode(blob) == u["ciphertext"]
            assert (
                b"".join(lc.iter_decrypt_upload(key, u["upload_id"], [blob])) == plain
            )


_K = bytes(32)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda k: lc.seal(k, b"x", "a"), id="seal"),
        pytest.param(
            lambda k: lc.open_sealed(k, lc.b64u_encode(bytes(40)), "a"),
            id="open_sealed",
        ),
        pytest.param(lambda k: lc.encrypt_upload(k, "u1", b"x"), id="encrypt_upload"),
        pytest.param(
            lambda k: next(lc.iter_decrypt_upload(k, "u1", [bytes(40)])),
            id="iter_decrypt_upload",
        ),
    ],
)
def test_a_16_byte_key_is_refused(call):
    # AESGCM would quietly accept it as AES-128; link-v1 is AES-256 only.
    with pytest.raises(ValueError, match="key must be 32 bytes"):
        call(bytes(16))


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lc.generate_key_pairs, id="generate_key_pairs"),
        pytest.param(lambda: lc.public_from_private(_K, _K), id="public_from_private"),
        pytest.param(lambda: lc.pair_keys(_K, _K, "h", "d"), id="pair_keys"),
        pytest.param(lambda: lc.seal(_K, b"x", "a"), id="seal"),
        pytest.param(
            lambda: lc.open_sealed(_K, lc.b64u_encode(bytes(28)), "a"),
            id="open_sealed",
        ),
        pytest.param(lambda: lc.sign(_K, b"m"), id="sign"),
        pytest.param(
            lambda: lc.verify(_K, b"m", lc.b64u_encode(bytes(64))), id="verify"
        ),
        pytest.param(lambda: lc.encrypt_upload(_K, "u1", b"x"), id="encrypt_upload"),
        pytest.param(
            lambda: next(lc.iter_decrypt_upload(_K, "u1", [b""])),
            id="iter_decrypt_upload",
        ),
    ],
)
def test_without_the_extra_every_entry_point_names_the_install(monkeypatch, call):
    real_import = builtins.__import__

    def no_cryptography(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cryptography)
    with pytest.raises(lc.LinkCryptoUnavailable) as exc:
        call()
    assert 'pip install "magent-multi-ai-agents-manager[link]"' in str(exc.value)
