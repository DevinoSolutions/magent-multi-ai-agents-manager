"""A character the stdout cannot encode prints as an escape, never a crash.

On Windows a REDIRECTED stdout -- a pipe, a file, the Session-0 hand-off's
out.txt, the ssh channel `magent attach` reads -- is the ANSI code page with
strict errors. One project name or path it could not hold (a CJK or emoji
title) raised UnicodeEncodeError out of click.echo and exited 1, and `up`
got that far only after its sessions were already created.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from magent.cli import main

# Escapes, so this file stays ASCII (ruff RUF001 flags the glyphs).
ACCENTED = "café"
CJK = "中文"


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


@pytest.mark.skipif(sys.platform != "win32", reason="the switch is a no-op off Windows")
class TestTheUtf8ConsoleSwitch:
    def test_switching_to_utf8_keeps_the_escape(self, monkeypatch):
        import ctypes

        from magent.cli.ui import _force_utf8_console

        # Stubbed, not called: the real SetConsoleOutputCP changes the code
        # page of the console this suite runs in, and that outlives the run.
        kernel32 = SimpleNamespace(SetConsoleOutputCP=lambda _cp: 1)
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32))
        stream = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="backslashreplace"
        )
        monkeypatch.setattr(sys, "stdout", stream)

        _force_utf8_console()

        # reconfigure(encoding=...) alone resets errors to strict, which
        # would quietly undo the entry point's escape for `magent mobile`.
        assert (stream.encoding, stream.errors) == ("utf-8", "backslashreplace")
