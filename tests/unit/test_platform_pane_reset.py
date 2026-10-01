"""pane_reset_command: the ABC default, the FakePlatform recorder, and the real
Windows PowerShell reset (the alt-screen-off sequence + the quoted notice)."""

from __future__ import annotations

import sys

import pytest

from magent import reap
from magent.platform import Platform, get_platform
from tests.conftest import FakePlatform
from tests.unit._ps_parse import parse as parse_powershell
from tests.unit.test_reap_park import _sig

win32_only = pytest.mark.skipif(
    sys.platform != "win32", reason="platform/windows.py imports only on win32"
)

# The spec's step-6 line, verbatim, up to the quoted notice (poc-reap2 A4).
_RESET = (
    "$e=[char]27; [Console]::Write("
    '"$e[?1000l$e[?1002l$e[?1003l$e[?1006l$e[?1004l$e[?2004l'
    '$e[<u$e[>4;0m$e[?1049l$e[?25h"); '
    "Clear-Host; Write-Host "
)


def _windows():
    from magent.platform.windows import WindowsPlatform

    return WindowsPlatform()


def _assert_one_literal(line: str | None, notice: str, tmp_path) -> None:
    assert line is not None
    parsed = parse_powershell(line, tmp_path)
    assert parsed.errors == []
    assert parsed.named("Get-Date") == []
    assert parsed.named("Write-Host") == [
        [("const", "BareWord", "Write-Host"), ("const", "SingleQuoted", notice)]
    ]


class TestTheDefaultIsNoReset:
    def test_the_abc_answers_none(self):
        # Called unbound, so the answer is the ABC's own on every OS.
        assert Platform.pane_reset_command(FakePlatform(), "pwsh.exe", "n") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="windows overrides it")
    def test_macos_and_linux_take_the_default(self):
        assert get_platform().pane_reset_command("pwsh.exe", "notice") is None


class TestTheFakeRecordsAndAnswers:
    def test_it_records_and_returns_the_canned_command(self):
        plat = FakePlatform(pane_reset="RESET-CMD")
        assert plat.pane_reset_command("pwsh.exe", "notice text") == "RESET-CMD"
        assert plat.pane_resets == [("pwsh.exe", "notice text")]

    def test_with_nothing_canned_it_answers_none_and_still_records(self):
        plat = FakePlatform()
        assert plat.pane_reset_command("cmd.exe", "n") is None
        assert plat.pane_resets == [("cmd.exe", "n")]


@win32_only
class TestTheWindowsReset:
    @pytest.mark.parametrize(
        "shell_image",
        ["C:/x/pwsh.exe", "C:\\Program Files\\PowerShell\\7\\PWSH.EXE", "pwsh"],
    )
    def test_a_pwsh_stem_gets_the_exact_line(self, shell_image):
        cmd = _windows().pane_reset_command(shell_image, "hello there")
        assert cmd == _RESET + "'hello there'"

    def test_a_windows_powershell_stem_gets_it_too(self):
        cmd = _windows().pane_reset_command(
            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "hi"
        )
        assert cmd == _RESET + "'hi'"

    def test_the_line_turns_the_alt_screen_and_the_input_modes_off(self):
        cmd = _windows().pane_reset_command("pwsh.exe", "n")
        assert cmd is not None
        assert "?1049l" in cmd  # alt-screen OFF -- the frozen frame goes
        assert "?2004l" in cmd  # bracketed paste OFF
        assert "?25h" in cmd  # cursor shown

    def test_the_notice_is_one_literal_nothing_in_it_expands(self):
        # A quote, a `$` and a backtick must reach the screen as written: the
        # notice names a session id, and a pane must never run a piece of it.
        cmd = _windows().pane_reset_command("pwsh.exe", "it's $x `n; exit")
        assert cmd == _RESET + "'it''s $x `n; exit'"

    @pytest.mark.parametrize("quote", ["'", "\u2018", "\u2019", "\u201a", "\u201b"])
    def test_a_notice_holding_a_quote_parses_as_that_one_literal(self, tmp_path, quote):
        # PowerShell's own parser, never a run: Write-Host gets exactly ONE
        # argument, the single-quoted notice itself, and no fragment of the
        # notice parses as a command of its own.
        notice = f"a {quote}; Get-Date; {quote}"
        _assert_one_literal(
            _windows().pane_reset_command("pwsh.exe", notice), notice, tmp_path
        )

    def test_a_configured_cmd_holding_a_quote_is_in_the_one_literal(self, tmp_path):
        # The notice carries the configured cmd verbatim (the resume command
        # only swaps the resume flag), so a typographic quote in the config
        # reaches the typed line.
        sig = _sig(cmd="claude --continue --name \u2019; Get-Date; \u2019")
        notice = reap._notice(sig)
        assert "\u2019; Get-Date; \u2019" in notice  # the control: it is in the line
        _assert_one_literal(
            _windows().pane_reset_command("pwsh.exe", notice), notice, tmp_path
        )

    def test_a_configured_cmd_holding_control_characters_types_none(self):
        # The whole line is typed as keystrokes: the ESC the reset needs is
        # spelled [char]27, and the notice drops a command that holds any.
        sig = _sig(cmd="claude --continue \x1b[2J\r\x03\x9b")
        line = _windows().pane_reset_command("pwsh.exe", reap._notice(sig))
        assert line is not None
        assert [ch for ch in line if ch < " " or "\x7f" <= ch <= "\x9f"] == []

    @pytest.mark.parametrize(
        "shell_image", ["cmd.exe", "C:/Program Files/Git/bin/bash.exe", "nu.exe", ""]
    )
    def test_a_shell_without_a_scripted_reset_gets_none(self, shell_image):
        assert _windows().pane_reset_command(shell_image, "x") is None
