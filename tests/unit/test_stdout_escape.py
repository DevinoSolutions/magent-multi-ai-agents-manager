"""A character the stdout cannot encode prints as an escape, never a crash.

On Windows a REDIRECTED stdout -- a pipe, a file, the Session-0 hand-off's
out.txt, the ssh channel `magent attach` reads -- is the ANSI code page with
a handler that raises on it. One project name or path it could not hold (a
CJK or emoji title) raised UnicodeEncodeError out of click.echo and exited 1,
and `up` got that far only after its sessions were already created.

The escape is ``magent.escape``, not plain backslashreplace: a lone
U+DC80..U+DCFF is a byte surrogateescape decoded (a POSIX path that is not
UTF-8), and it must still be written back as that byte.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from magent.cli import main
from magent.cli.app import OUTPUT_ERRORS, _escape_unencodable_output

# Escapes, so this file stays ASCII.
ACCENTED = "caf\u00e9"
CJK = "\u4e2d\u6587"

# Every kind of character the handler tells apart, ADJACENT, so one encoder
# call is handed all of them as a single unencodable run: a CJK character, an
# escaped byte, an astral emoji, another escaped byte, a HIGH surrogate.
MIXED_RUN = "a\u4e2d\udc80\U0001f600\udcff\ud800b"
MIXED_RUN_CP1252 = b"a\\u4e2d\x80\\U0001f600\xff\\ud800b"


@pytest.fixture
def stdout_as(monkeypatch):
    """A real TextIOWrapper as ``sys.stdout``, put through the entry point's
    reconfigure exactly as the group callback does. Returns the stream and
    the bytes object behind it."""

    def _install(encoding: str, errors: str) -> tuple[io.TextIOWrapper, io.BytesIO]:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding=encoding, errors=errors)
        monkeypatch.setattr(sys, "stdout", stream)
        _escape_unencodable_output()
        return stream, raw

    return _install


def _written(stream: io.TextIOWrapper, raw: io.BytesIO, text: str) -> bytes:
    stream.write(text)
    stream.flush()
    return raw.getvalue()


class TestTheEntryPoint:
    def test_a_legacy_code_page_stdout_escapes_only_what_it_cannot_hold(
        self, tmp_config, tmp_path
    ):
        cfg = tmp_config({"projects": [{"path": str(tmp_path / f"{ACCENTED} {CJK}")}]})

        # CliRunner's stdout is a strict TextIOWrapper in the charset it is
        # given: exactly a redirected Windows stdout. `config show` does
        # nothing about encodings itself, so this is the group callback.
        result = CliRunner(charset="cp1252").invoke(
            main, ["--config", cfg, "config", "show"]
        )

        assert result.exit_code == 0, result.exception
        # The accent cp1252 has is written exactly as before; only the two
        # characters it lacks become escapes.
        assert f"{ACCENTED} \\u4e2d\\u6587" in result.stdout

    @pytest.mark.parametrize(
        "stream", [None, io.StringIO()], ids=["pythonw-no-stdout", "no-reconfigure"]
    )
    def test_a_stdout_it_cannot_reconfigure_is_left_alone(self, monkeypatch, stream):
        from magent.cli.app import _escape_unencodable_output

        monkeypatch.setattr(sys, "stdout", stream)

        _escape_unencodable_output()

        assert sys.stdout is stream


class TestEachCharacterGetsItsOwnAnswer:
    def test_an_adjacent_mixed_run_one_shot(self, stdout_as):
        stdout_as("cp1252", "strict")  # registers the handler, as the callback does

        # One handler call is handed the whole run; answering it all as one
        # kind would either escape the two bytes as text or refuse the rest.
        assert MIXED_RUN.encode("cp1252", OUTPUT_ERRORS) == MIXED_RUN_CP1252

    def test_the_same_run_through_the_reconfigured_stream(self, stdout_as):
        stream, raw = stdout_as("cp1252", "strict")

        assert _written(stream, raw, MIXED_RUN) == MIXED_RUN_CP1252

    @pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
    @pytest.mark.parametrize(
        ("char", "expected"),
        [
            # The escaped-byte range, both ends: the byte it was decoded from.
            ("\udc80", b"\x80"),
            ("\udcff", b"\xff"),
            # Every other lone surrogate is just an unencodable character.
            ("\ud800", b"\\ud800"),
            ("\udbff", b"\\udbff"),
            ("\udc00", b"\\udc00"),
            ("\udc7f", b"\\udc7f"),
            ("\udd00", b"\\udd00"),
            ("\udfff", b"\\udfff"),
        ],
        ids=["dc80", "dcff", "d800", "dbff", "dc00", "dc7f", "dd00", "dfff"],
    )
    def test_only_dc80_to_dcff_come_back_as_bytes(
        self, stdout_as, encoding, char, expected
    ):
        stream, raw = stdout_as(encoding, "strict")

        assert _written(stream, raw, f"<{char}>") == b"<" + expected + b">"


class TestWhichStreamsItChanges:
    @pytest.mark.parametrize("errors", ["strict", "surrogateescape"])
    def test_a_default_handler_is_replaced_and_the_encoding_kept(
        self, stdout_as, errors
    ):
        stream, raw = stdout_as("cp1252", errors)

        # KEEPING the encoding is load-bearing: a Python parent that reads
        # magent with text=True decodes in its locale encoding, so the accent
        # must stay cp1252's own byte, not become UTF-8.
        assert (stream.encoding, stream.errors) == ("cp1252", OUTPUT_ERRORS)
        assert _written(stream, raw, f"{ACCENTED} {CJK}") == (b"caf\xe9 \\u4e2d\\u6587")

    def test_a_posix_path_byte_is_written_back_unchanged(self, stdout_as):
        # Python's UTF-8 mode (and a C.UTF-8 locale) gives stdout utf-8 +
        # surrogateescape, and decodes a non-UTF-8 byte in a path to a lone
        # U+DC80..U+DCFF. Today that prints as the real path; plain
        # backslashreplace would print "\udcff" instead.
        stream, raw = stdout_as("utf-8", "surrogateescape")

        assert _written(stream, raw, "/srv/x\udcff.json") == b"/srv/x\xff.json"

    @pytest.mark.parametrize(
        "errors", ["replace", "ignore", "backslashreplace", "xmlcharrefreplace"]
    )
    def test_a_handler_somebody_chose_is_left_alone(self, stdout_as, errors):
        # PYTHONIOENCODING=cp1252:replace, or a harness's own wrapper: a
        # deliberate choice, and not one this process gets to overrule.
        stream, raw = stdout_as("cp1252", errors)

        assert stream.errors == errors
        assert _written(stream, raw, CJK) == CJK.encode("cp1252", errors)


@pytest.mark.skipif(sys.platform != "win32", reason="the switch is a no-op off Windows")
class TestTheUtf8ConsoleSwitch:
    @pytest.fixture(autouse=True)
    def _no_console_code_page(self, monkeypatch):
        import ctypes

        # Stubbed, not called: the real SetConsoleOutputCP changes the code
        # page of the console this suite runs in, and that outlives the run.
        kernel32 = SimpleNamespace(SetConsoleOutputCP=lambda _cp: 1)
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32))

    def test_mobile_keeps_the_entry_points_handler(self, stdout_as):
        from magent.cli.ui import _force_utf8_console

        # The real order: the group callback runs, then `magent mobile`
        # switches the console to UTF-8.
        stream, raw = stdout_as("cp1252", "strict")

        _force_utf8_console()

        # Reset to strict, the lone surrogates below would raise; hard-coded
        # to backslashreplace, the escaped byte would print as text.
        assert (stream.encoding, stream.errors) == ("utf-8", OUTPUT_ERRORS)
        assert _written(stream, raw, "\udcff\ud800" + CJK) == (
            b"\xff\\ud800" + CJK.encode("utf-8")
        )

    def test_switching_to_utf8_keeps_the_escape(self, monkeypatch):
        from magent.cli.ui import _force_utf8_console

        stream = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="backslashreplace"
        )
        monkeypatch.setattr(sys, "stdout", stream)

        _force_utf8_console()

        # reconfigure(encoding=...) alone resets errors to strict, which
        # would quietly undo the entry point's escape for `magent mobile`.
        assert (stream.encoding, stream.errors) == ("utf-8", "backslashreplace")
