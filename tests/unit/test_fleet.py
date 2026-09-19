"""Tests for magent.fleet: footer/state parsing, name resolution, and the
psmux send/switch choreography -- the last of these driven against a REAL fake
psmux binary so the ``send-keys -l`` wire and slash-command safety are proven,
not just the argv magent builds."""

from __future__ import annotations

import subprocess
import time

import pytest

from magent import fleet, psmux
from tests.unit._fake_psmux import make_fake_psmux

MID = "\u00b7"  # the footer separator Claude Code paints
CARET = "\u276f"  # the menu selection caret


class TestParseFooter:
    def test_reads_model_and_effort(self):
        assert fleet.parse_footer(f"Fable 5.1 {MID} high") == ("Fable 5.1", "high")

    def test_handles_multi_token_model_names(self):
        assert fleet.parse_footer(f"  Opus 5 {MID} xhigh  ") == ("Opus 5", "xhigh")

    def test_no_footer_is_none_none(self):
        assert fleet.parse_footer("just some pane text") == (None, None)

    def test_takes_the_last_footer_in_the_capture(self):
        pane = f"Sonnet 4.5 {MID} low\nwork...\nOpus 5 {MID} max"
        assert fleet.parse_footer(pane) == ("Opus 5", "max")

    def test_empty_pane_is_none_none(self):
        assert fleet.parse_footer("") == (None, None)


class TestClassifyState:
    def test_empty_is_nopane(self):
        assert fleet.classify_state("   \n ") == "nopane"

    def test_bare_shell_is_idle(self):
        assert fleet.classify_state(f"PS C:\\p> claude\nFable 5.1 {MID} high") == "idle"

    def test_spinner_is_busy(self):
        assert fleet.classify_state("* Working (12s * esc to interrupt)") == "busy"

    def test_dialog_prompt_is_dialog(self):
        assert (
            fleet.classify_state(f"Do you want to proceed?\n{CARET} 1. Yes") == "dialog"
        )

    def test_usage_limit_is_limit(self):
        assert fleet.classify_state("Claude usage limit reached; try later") == "limit"

    def test_dialog_outranks_busy(self):
        # A confirm prompt with a stray timer in scrollback is a dialog: the
        # user is the blocker, not the clock.
        assert fleet.classify_state("(3s\nDo you want to continue?") == "dialog"

    def test_busy_outranks_a_stale_limit_line(self):
        assert (
            fleet.classify_state("approaching usage limit\n* thinking esc to interrupt")
            == "busy"
        )


_RESOLVE_NAMES = ["caramel", "upup", "quora-upsell"]


class TestResolveSession:
    def test_exact_case_insensitive(self):
        assert fleet.resolve_session("CARAMEL", _RESOLVE_NAMES) == "caramel"

    def test_unique_substring(self):
        assert fleet.resolve_session("cara", _RESOLVE_NAMES) == "caramel"

    def test_unique_prefix_when_substring_is_ambiguous(self):
        # "u" is inside both upup and quora-upsell (ambiguous substring) but a
        # prefix of only upup.
        assert fleet.resolve_session("u", _RESOLVE_NAMES) == "upup"

    def test_ambiguous_is_none(self):
        assert fleet.resolve_session("up", ["upup", "upsell"]) is None

    def test_absent_is_none(self):
        assert fleet.resolve_session("zzz", _RESOLVE_NAMES) is None

    def test_empty_query_is_none(self):
        assert fleet.resolve_session("", _RESOLVE_NAMES) is None


class TestFlatten:
    def test_collapses_newlines_to_spaces(self):
        assert (
            fleet.flatten("line one\n   line two\n\tthree") == "line one line two three"
        )

    def test_strips_edges(self):
        assert fleet.flatten("  hi  ") == "hi"


class TestLooksUnsent:
    def test_prompt_still_on_last_line_is_unsent(self):
        pane = "some scrollback\n> Please refactor the parser thoroughly"
        assert fleet.looks_unsent(pane, "Please refactor the parser thoroughly") is True

    def test_prompt_gone_from_last_line_is_sent(self):
        pane = "Please refactor the parser\n...working on it now"
        assert (
            fleet.looks_unsent(pane, "Please refactor the parser thoroughly") is False
        )

    def test_very_short_prompt_is_unverifiable(self):
        # Too short to tell "unsent" from "echoed"; never a false failure.
        assert fleet.looks_unsent("> hi", "hi") is False


class TestVerifySwitch:
    def test_model_and_effort_match(self):
        assert fleet.verify_switch(f"Opus 5 {MID} xhigh", "opus", "xhigh") is True

    def test_wrong_effort_fails(self):
        assert fleet.verify_switch(f"Opus 5 {MID} high", "opus", "xhigh") is False

    def test_wrong_model_fails(self):
        assert fleet.verify_switch(f"Fable 5.1 {MID} high", "opus", "high") is False

    def test_unknown_model_verifies_on_effort_only(self):
        assert fleet.verify_switch(f"Custom 9 {MID} high", "custom", "high") is True

    def test_no_footer_fails(self):
        assert fleet.verify_switch("no footer here", "opus", "high") is False

    def test_model_only_when_no_effort_requested(self):
        assert fleet.verify_switch(f"Opus 5 {MID} low", "opus", None) is True


class TestPasteAndEnterWire:
    """The argv magent builds, asserted through a subprocess.run fake -- the
    same fake-psmux boundary the rest of the psmux suite uses."""

    def _record(self, monkeypatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def _run(cmd, **kwargs):
            calls.append(list(cmd))

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(subprocess, "run", _run)
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        return calls

    def test_literal_flag_and_verbatim_slash_command(self, monkeypatch):
        calls = self._record(monkeypatch)
        assert fleet.paste_and_enter("api", "/model opus 5", psmux_bin="psmux") is True
        paste, enter = calls
        # The literal paste carries -l and the slash-command as ONE argv token,
        # so no shell can rewrite the leading slash into a Windows path.
        assert paste == [
            "psmux",
            "-L",
            "api",
            "send-keys",
            "-t",
            "api",
            "-l",
            "--",
            "/model opus 5",
        ]
        # Enter is a real key name, sent separately, WITHOUT -l.
        assert enter == ["psmux", "-L", "api", "send-keys", "-t", "api", "--", "Enter"]

    def test_a_failed_paste_never_presses_enter(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        class _Fail:
            returncode = 1

        seen: list[list[str]] = []

        def _run(cmd, **kwargs):
            seen.append(list(cmd))
            return _Fail()

        monkeypatch.setattr(subprocess, "run", _run)
        assert fleet.paste_and_enter("api", "hello there", psmux_bin="psmux") is False
        assert len(seen) == 1  # paste attempted, Enter never sent


class TestWaitForIdle:
    def test_returns_true_once_idle(self, monkeypatch):
        panes = iter(
            ["* busy (2s esc to interrupt)", f"PS> claude\nFable 5.1 {MID} high"]
        )
        monkeypatch.setattr(psmux, "capture_pane", lambda name, psmux=None: next(panes))
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        assert fleet.wait_for_idle("api", deadline=time.monotonic() + 100) is True

    def test_timeout_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            psmux, "capture_pane", lambda name, psmux=None: "* busy esc to interrupt"
        )
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        # Deadline already passed: the loop body is skipped and the final read
        # is still busy.
        assert fleet.wait_for_idle("api", deadline=time.monotonic() - 1) is False


class TestSwitchModel:
    def _recorder(self, monkeypatch, *, ok=True):
        sent: list[str] = []

        def _send(name, *keys, target=None, literal=False, psmux=None, timeout=None):
            sent.append(keys[0])
            return ok

        monkeypatch.setattr(psmux, "send_keys", _send)
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        return sent

    def test_sends_model_then_effort_in_order(self, monkeypatch):
        sent = self._recorder(monkeypatch)
        assert fleet.switch_model("api", "opus", "xhigh", psmux_bin="psmux") is True
        assert sent == ["/model opus", "Enter", "/effort xhigh", "Enter"]

    def test_model_only_when_no_effort(self, monkeypatch):
        sent = self._recorder(monkeypatch)
        assert fleet.switch_model("api", "fable", None, psmux_bin="psmux") is True
        assert sent == ["/model fable", "Enter"]

    def test_stops_and_reports_false_on_a_failed_send(self, monkeypatch):
        sent = self._recorder(monkeypatch, ok=False)
        assert fleet.switch_model("api", "opus", "high", psmux_bin="psmux") is False
        # First paste failed -> no Enter, no /effort.
        assert sent == ["/model opus"]


class TestAgainstARealFakePsmuxBinary:
    """End-to-end through a genuine on-disk fake psmux executable: the literal
    text and the slash-command reach a real process exactly as typed."""

    def test_paste_and_enter_reaches_the_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        fake = make_fake_psmux(tmp_path)
        assert fleet.paste_and_enter("api", "/model opus", psmux_bin=fake.path) is True
        sends = fake.send_key_calls()
        assert len(sends) == 2
        assert sends[0] == [
            "-L",
            "api",
            "send-keys",
            "-t",
            "api",
            "-l",
            "--",
            "/model opus",
        ]
        assert sends[1] == ["-L", "api", "send-keys", "-t", "api", "--", "Enter"]

    def test_read_state_parses_the_binarys_pane(self, tmp_path):
        fake = make_fake_psmux(tmp_path, pane=f"PS> claude\nOpus 5 {MID} max")
        state = fleet.read_state("api", psmux_bin=fake.path)
        assert state == {"state": "idle", "model": "Opus 5", "effort": "max"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
