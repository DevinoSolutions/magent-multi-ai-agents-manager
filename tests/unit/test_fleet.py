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
CARET = "\u276f"  # the menu selection caret AND the input-line caret
RULE = "\u2500" * 62
TYPED_RULE = "\u2500" * 12  # a rule the user typed: narrower than the box's


def real_pane(typed: str = "", *, effort: str = "high") -> str:
    """The bottom of a REAL Claude Code pane, captured read-only from a live
    session: rule, the caret INPUT line, rule, footer, hints row. The hints row
    being last is exactly why ``looks_unsent`` cannot read the last line."""
    return "\n".join(
        [
            "  I'll start by reading the repo's key files.",
            "",
            RULE,
            f"{CARET} {typed}".rstrip(),
            RULE,
            f"  Fable 5.1 {MID} {effort} {MID} 221K/550K {MID} ai-agent-notify",
            (
                f"  \u23f5\u23f5 bypass permissions on (shift+tab to cycle) {MID} "
                "\u2190 for agents"
            ),
        ]
    )


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


class TestLooksUnsentOnARealClaudeCodePane:
    """The geometry that made exit 4 unreachable: on a real pane the input line
    sits FOUR rows above the bottom, under a rule/footer/hints stack."""

    PROMPT = "this text stays unsent 4242"

    def test_prompt_on_the_caret_line_is_unsent(self):
        assert fleet.looks_unsent(real_pane(self.PROMPT), self.PROMPT) is True

    def test_empty_caret_line_is_sent(self):
        assert fleet.looks_unsent(real_pane(), self.PROMPT) is False

    def test_the_caret_line_is_found_not_the_last_line(self):
        pane = real_pane(self.PROMPT)
        assert fleet.input_line(pane) == f"{CARET} {self.PROMPT}"
        assert "for agents" in pane.splitlines()[-1]

    def test_a_pane_with_no_caret_falls_back_to_the_last_line(self):
        # A bare shell or an agent mid-boot has no input line to find, and the
        # last line is then the only signal there is.
        assert fleet.input_line("PS C:\\p> claude\nstarting...") is None
        assert fleet.looks_unsent(f"PS C:\\p> {self.PROMPT}", self.PROMPT) is True

    def test_flattened_prompt_matches_the_single_echoed_line(self):
        # magent flattens newlines before pasting, so the pane shows one line.
        multi = "this text stays\n   unsent 4242"
        assert fleet.looks_unsent(real_pane(fleet.flatten(multi)), multi) is True


class TestInputDraft:
    def test_an_empty_input_line_is_empty_string(self):
        assert fleet.input_draft(real_pane("")) == ""

    def test_a_draft_is_returned_stripped(self):
        assert fleet.input_draft(real_pane("fix the tests")) == "fix the tests"

    def test_a_lone_numbered_draft_is_a_draft(self):
        # "1. fix the tests" typed at the prompt, with no sibling options.
        assert fleet.input_draft(real_pane("1. fix the tests")) == "1. fix the tests"

    def test_a_numbered_menu_has_no_input_line(self):
        pane = "\n".join(
            [
                "  Do you want to proceed?",
                f"{CARET} 1. Yes",
                "  2. No",
                RULE,
            ]
        )
        assert fleet.input_draft(pane) is None

    def test_a_menu_with_the_highlight_on_the_last_option_has_no_input_line(self):
        pane = "\n".join(
            [
                "  1. Yes",
                f"{CARET} 2. No",
                RULE,
            ]
        )
        assert fleet.input_draft(pane) is None

    def test_no_caret_at_all_is_none(self):
        assert fleet.input_draft("just some text\nno caret here") is None

    def test_empty_capture_is_none(self):
        assert fleet.input_draft("") is None

    def test_a_draft_below_an_empty_caret_line_is_a_draft(self):
        # A multi-line draft whose first line is empty: the caret line alone
        # reads "", the box does not.
        pane = real_pane("").replace(f"{CARET}\n", f"{CARET}\n  fix the tests\n")
        assert fleet.input_draft(pane) == "fix the tests"

    def test_every_line_of_a_multiline_draft_is_kept(self):
        pane = real_pane("first line").replace(
            "first line\n", "first line\n  second line\n"
        )
        assert fleet.input_draft(pane) == "first line\nsecond line"

    def test_blank_lines_in_the_box_are_no_draft(self):
        pane = real_pane("").replace(f"{CARET}\n", f"{CARET}\n   \n")
        assert fleet.input_draft(pane) == ""

    def test_a_blank_line_does_not_close_the_box(self):
        pane = real_pane("").replace(f"{CARET}\n", f"{CARET}\n\n  fix the tests\n")
        assert fleet.input_draft(pane) == "fix the tests"

    def test_a_box_with_no_closing_rule_is_none(self):
        # Cut off below the caret line: whatever was below it is unknown.
        assert fleet.input_draft("\n".join([RULE, f"{CARET}"])) is None
        assert fleet.input_draft("\n".join([RULE, f"{CARET}", "  more"])) is None

    def test_a_line_with_text_inside_the_rule_does_not_close_the_box(self):
        pane = "\n".join([RULE, f"{CARET}", f"{RULE[:8]} note {RULE[:8]}"])
        assert fleet.input_draft(pane) is None

    @pytest.mark.parametrize("lone", [CARET, f"  {CARET}"], ids=["col0", "indented"])
    def test_a_draft_ending_in_a_lone_caret_line_is_a_draft(self, lone):
        # The last caret line is not the box's first line: the box is read
        # from its top rule, not from the last caret.
        pane = real_pane("fix the tests").replace(
            "fix the tests\n", f"fix the tests\n{lone}\n"
        )
        assert fleet.input_draft(pane) == f"fix the tests\n{CARET}"

    @pytest.mark.parametrize(
        ("box", "draft"),
        [
            pytest.param(
                [f"{CARET} fix the tests", TYPED_RULE, CARET],
                f"fix the tests\n{TYPED_RULE}\n{CARET}",
                id="rule-then-lone-caret",
            ),
            pytest.param(
                [CARET, TYPED_RULE, "  fix the tests"],
                f"{TYPED_RULE}\nfix the tests",
                id="empty-first-line-then-rule",
            ),
            pytest.param(
                [
                    f"{CARET} look at this:",
                    f"  {TYPED_RULE}",
                    f"  {CARET}",
                    f"  {TYPED_RULE}",
                ],
                f"look at this:\n{TYPED_RULE}\n{CARET}\n{TYPED_RULE}",
                id="pasted-box-fragment",
            ),
            # A typed rule that fills a continuation line: with the line's
            # two-column indent it is as wide as the box, so only the stripped
            # width tells it from an edge.
            pytest.param(
                [f"{CARET} fix", "  " + "─" * (len(RULE) - 2), "  more"],
                "fix\n" + "─" * (len(RULE) - 2) + "\nmore",
                id="rule-filling-an-indented-line",
            ),
        ],
    )
    def test_a_rule_in_the_draft_is_draft_text(self, box, draft):
        # Claude Code draws the box's rules the full width of the pane (by
        # analogy: a read-only capture of a live 50-column pane showed a dialog,
        # not the input box, and every rule in it was 50 wide), so a narrower
        # rule inside the box is text the user typed or pasted. Read as a box
        # edge, it cut the draft short -- to "" in each shape here.
        pane = "\n".join([RULE, *box, RULE, f"  Fable 5.1 {MID} high"])
        assert fleet.input_draft(pane) == draft

    @pytest.mark.parametrize(
        "box",
        [
            [CARET, RULE, "  fix the tests"],
            [f"{CARET} fix", RULE, "  more"],
            [f"{CARET} fix", RULE],
        ],
        ids=["empty-first-line", "after-a-line", "last-line"],
    )
    def test_a_pane_wide_rule_typed_into_the_draft_is_none(self, box):
        # A typed rule exactly as wide as the box cannot be told from its edge.
        # Closing the box there hides the lines under it (the first shape read
        # "", no draft at all), so the box must close at the pane's last rule.
        pane = "\n".join([RULE, *box, RULE, f"  Fable 5.1 {MID} high"])
        assert fleet.input_draft(pane) is None

    @pytest.mark.parametrize(
        "above", [TYPED_RULE, f"{RULE}──"], ids=["narrower", "wider"]
    )
    def test_the_box_is_as_wide_as_its_bottom_edge_not_the_transcript(self, above):
        # A rule in the scrollback -- output, or the box as it was drawn before
        # the pane was resized -- sets no width: the pane's last rule does.
        pane = "\n".join(["  results:", above, "  a table row", "", real_pane("fix")])
        assert fleet.input_draft(pane) == "fix"

    def test_a_rule_in_the_transcript_above_the_box_does_not_open_it(self):
        # The box opens at the NEAREST rule above its caret line.
        pane = "\n".join(["  results:", RULE, "  a table row", "", real_pane("fix")])
        assert fleet.input_draft(pane) == "fix"

    def test_a_box_with_no_top_rule_is_none(self):
        # Cut off above the caret line: whatever was above it is unknown.
        pane = "\n".join([f"{CARET} fix the tests", RULE, f"  Fable 5.1 {MID} high"])
        assert fleet.input_draft(pane) is None

    def test_a_box_whose_first_line_is_not_the_caret_line_is_none(self):
        # The rule above the last caret line is not the input box's top rule.
        pane = "\n".join([RULE, "  stray text", CARET, RULE])
        assert fleet.input_draft(pane) is None

    def test_a_busy_pane(self):
        busy = "\n".join(
            [
                "* Working (12s * esc to interrupt)",
                RULE,
                f"{CARET} ",
                RULE,
                f"  Fable 5.1 {MID} high",
            ]
        )
        assert fleet.classify_state(busy) == "busy"
        assert fleet.input_draft(busy) == ""
        typed = busy.replace(f"{CARET} ", f"{CARET} queue this next")
        assert fleet.input_draft(typed) == "queue this next"


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

    @pytest.fixture(autouse=True)
    def _patient_capture(self, monkeypatch):
        # The capture budget in these tests only. The fake is a Python shim;
        # on a loaded Windows box its start alone has overrun the product's 3s,
        # and a green parse then failed as "nopane". What a capture timeout
        # DOES is pinned below with its own, tiny budget.
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 60.0)

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

    def test_a_pane_slower_than_the_budget_reads_timeout_not_nopane(
        self, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(tmp_path, pane=f"PS> claude\nOpus 5 {MID} max")
        fake.set_capture_delay(1.5)
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)
        state = fleet.read_state("api", psmux_bin=fake.path)
        assert state == {"state": "timeout", "model": None, "effort": None}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
