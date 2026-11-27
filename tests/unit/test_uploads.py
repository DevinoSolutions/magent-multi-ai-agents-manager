"""uploads: save + paste lifted out of the HTTP handler, contract unchanged."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from magent import uploads


@pytest.fixture
def with_psmux(monkeypatch):
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")


def _save(tmp_path, files, *, inject=True, paste=(True, False), calls=None):
    def _inject(session, text):
        if calls is not None:
            calls.append((session, text))
        return paste

    return uploads.save(
        files,
        "caramel",
        inject=inject,
        upload_dir=tmp_path / "uploads",
        max_bytes=1000,
        inject_fn=_inject,
    )


class TestSave:
    def test_writes_every_file_in_order(self, tmp_path):
        result = _save(tmp_path, [("a.png", b"AAA"), ("b.txt", b"BB")], inject=False)
        assert [p.rsplit("_", 1)[-1] for p in result.paths] == ["a.png", "b.txt"]
        assert result.path == result.paths[0]
        assert Path(result.paths[1]).read_bytes() == b"BB"
        assert result.paste == "saved"
        assert len(result.upload_id) == 16

    def test_one_paste_for_every_file(self, tmp_path, with_psmux):
        calls = []
        result = _save(tmp_path, [("a.png", b"A"), ("b.png", b"B")], calls=calls)
        [(session, line)] = calls
        assert session == "caramel"
        assert all(path in line for path in result.paths)

    @pytest.mark.parametrize(
        ("flags", "state"),
        [
            ((True, False), "injected"),
            ((False, True), "pending"),
            ((False, False), "saved"),
        ],
    )
    def test_three_paste_states(self, tmp_path, with_psmux, flags, state):
        assert _save(tmp_path, [("a.png", b"A")], paste=flags).paste == state

    def test_no_psmux_saves_without_pasting(self, tmp_path, monkeypatch):
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        calls = []
        assert _save(tmp_path, [("a.png", b"A")], calls=calls).paste == "saved"
        assert calls == []

    def test_too_large_keeps_nothing(self, tmp_path):
        with pytest.raises(uploads.UploadError) as err:
            _save(tmp_path, [("a.bin", b"x" * 1001)])
        assert err.value.code == "payload_too_large"
        assert not (tmp_path / "uploads").exists()

    def test_no_files_is_invalid(self, tmp_path):
        with pytest.raises(uploads.UploadError) as err:
            _save(tmp_path, [])
        assert err.value.code == "invalid_request"
        assert err.value.message == "Missing file"

    def test_an_invalid_name_refuses_the_whole_request(self, tmp_path, monkeypatch):
        real = uploads.dest_for

        def _second_escapes(root, stamp, filename):
            return None if filename == "bad" else real(root, stamp, filename)

        monkeypatch.setattr(uploads, "dest_for", _second_escapes)
        with pytest.raises(uploads.UploadError, match="Invalid filename"):
            _save(tmp_path, [("good.png", b"A"), ("bad", b"B")])
        assert list((tmp_path / "uploads").iterdir()) == []

    def test_legacy_body_carries_two_flags(self, tmp_path, with_psmux):
        body = _save(tmp_path, [("a.png", b"A")], paste=(False, True)).legacy()
        assert body["ok"] is True
        assert (body["injected"], body["inject_pending"]) == (False, True)
        assert set(body) == {"ok", "path", "paths", "injected", "inject_pending"}


class TestInjectPaste:
    def test_pastes_literally_into_the_session_with_the_timeout(self, monkeypatch):
        calls = []

        def _send_keys(session, text, **kwargs):
            calls.append((session, text, kwargs))
            return True

        monkeypatch.setattr("magent.psmux.send_keys", _send_keys)
        assert uploads.inject_paste("caramel", "'a.png'", timeout_s=7.5) == (
            True,
            False,
        )
        [(session, text, kwargs)] = calls
        assert (session, text) == ("caramel", "'a.png'")
        assert kwargs["literal"] is True
        assert kwargs["target"] == "caramel"
        assert kwargs["timeout"] == 7.5

    def test_the_clocks_are_read_at_call_time(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "magent.psmux.send_keys",
            lambda *_a, **kw: seen.append(kw["timeout"]) or True,
        )
        monkeypatch.setattr(uploads, "INJECT_TIMEOUT_S", 11.0)
        uploads.inject_paste("caramel", "x")
        assert seen == [11.0]

    def test_a_slow_paste_is_pending_not_refused(self, monkeypatch):
        release = threading.Event()

        def _slow(*_a, **_kw):
            release.wait(5)
            return True

        monkeypatch.setattr("magent.psmux.send_keys", _slow)
        try:
            assert uploads.inject_paste("caramel", "x", grace_s=0.05) == (
                False,
                True,
            )
        finally:
            release.set()

    def test_a_refused_paste_is_neither(self, monkeypatch):
        monkeypatch.setattr("magent.psmux.send_keys", lambda *_a, **_kw: False)
        assert uploads.inject_paste("caramel", "x") == (False, False)


class TestUploadIncomplete:
    def test_is_an_invalid_request_with_the_byte_counts(self):
        err = uploads.UploadIncomplete("cut", received=3, declared=10)
        assert isinstance(err, uploads.UploadError)
        assert err.code == "invalid_request"
        assert (err.received, err.declared) == (3, 10)
        assert err.details == {"received": 3, "declared": 10}
        assert str(err) == "cut"


class TestParseMultipart:
    BOUNDARY = "----magentTest"

    def _body(self, *parts: tuple[str, str | None, bytes]) -> bytes:
        out = b""
        for name, filename, data in parts:
            disp = f'form-data; name="{name}"'
            if filename is not None:
                disp += f'; filename="{filename}"'
            out += (
                f"--{self.BOUNDARY}\r\nContent-Disposition: {disp}\r\n\r\n".encode()
                + data
                + b"\r\n"
            )
        return out + f"--{self.BOUNDARY}--\r\n".encode()

    def _parse(self, body: bytes, *, declared: int | None = None):
        return uploads.parse_multipart(
            f"multipart/form-data; boundary={self.BOUNDARY}",
            str(len(body) if declared is None else declared),
            io.BytesIO(body).read1,
            limit=10_000,
        )

    def test_fields_and_repeated_files(self):
        body = self._body(
            ("session", None, b"caramel"),
            ("file", "a.png", b"AAA"),
            ("file", "b.png", b"BBB"),
        )
        fields, files = self._parse(body)
        assert fields == {"session": "caramel"}
        assert [(n, bytes(d)) for n, d in files["file"]] == [
            ("a.png", b"AAA"),
            ("b.png", b"BBB"),
        ]

    def test_a_short_body_is_incomplete(self):
        body = self._body(("file", "a.png", b"AAA"))
        with pytest.raises(uploads.UploadIncomplete):
            self._parse(body, declared=len(body) + 50)
