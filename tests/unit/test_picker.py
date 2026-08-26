"""The shared type-to-filter picker: ranking, key decoding, and the state
machine -- all of it without a terminal.

The ranking half is exhaustive on purpose. It is the only part of this feature
a user experiences as a *judgement* ("why did it pick that one?"), and a
judgement that drifts between releases is worse than no ranking at all. Every
tier boundary and every tie-break rule below is a pin, not a sample.

The state-machine half is driven by synthetic key tokens -- the same strings
``read_key`` returns -- so the raw-terminal loop is the only untested line, and
the real-PTY tier covers that.
"""

from __future__ import annotations

import pytest

from magent.cli import picker


def _keys(state, *keys):
    """Press every key; return the first result, or None if none committed."""
    for k in keys:
        out = state.press(k)
        if out is not None:
            return out
    return None


def _state(labels, **kw):
    return picker.PickerState(
        [picker.PickerItem(key=str(i), label=n) for i, n in enumerate(labels, 1)], **kw
    )


class TestMatchTiers:
    @pytest.mark.parametrize(
        ("query", "candidate", "tier"),
        [
            # -- prefix
            ("web", "web-app", picker.PREFIX),
            ("WEB", "web-app", picker.PREFIX),
            ("web", "WEB-APP", picker.PREFIX),
            ("w", "web-app", picker.PREFIX),
            ("web-app", "web-app", picker.PREFIX),
            # -- word boundary: after a separator, or a camelCase seam
            ("app", "web-app", picker.BOUNDARY),
            ("app", "web_app", picker.BOUNDARY),
            ("app", "web app", picker.BOUNDARY),
            ("app", "web.app", picker.BOUNDARY),
            ("app", "web/app", picker.BOUNDARY),
            ("app", "webApp", picker.BOUNDARY),
            ("core", "svc-2-core", picker.BOUNDARY),
            # -- substring: present, but mid-word
            ("pp", "web-app", picker.SUBSTRING),
            ("eb", "web-app", picker.SUBSTRING),
            ("ppl", "grapple", picker.SUBSTRING),
            # -- subsequence: in order, not contiguous
            ("wp", "web-app", picker.SUBSEQUENCE),
            ("wba", "web-app", picker.SUBSEQUENCE),
            ("mgt", "magent", picker.SUBSEQUENCE),
            # -- no match
            ("z", "web-app", picker.NO_MATCH),
            ("paw", "web-app", picker.NO_MATCH),
            ("webb", "web-app", picker.NO_MATCH),
        ],
    )
    def test_tier(self, query, candidate, tier):
        assert picker.match_tier(query, candidate) == tier

    def test_an_empty_query_matches_everything_at_the_best_tier(self):
        assert picker.match_tier("", "anything") == picker.PREFIX
        assert picker.match_tier("   ", "anything") == picker.PREFIX

    def test_a_later_boundary_hit_beats_an_earlier_mid_word_one(self):
        # "app" appears mid-word first ("happy") and at a boundary second.
        # Scanning must find the BEST occurrence, not the first.
        assert picker.match_tier("app", "happy-app") == picker.BOUNDARY

    def test_prefix_wins_even_when_a_boundary_hit_exists_too(self):
        assert picker.match_tier("web", "web-web") == picker.PREFIX

    def test_surrounding_whitespace_in_the_query_is_ignored(self):
        assert picker.match_tier("  web  ", "web-app") == picker.PREFIX


class TestRankOrder:
    def test_tiers_order_the_results(self):
        # prefix, boundary, substring, subsequence -- one candidate each, fed
        # in the WRONG order so only the tiers can be doing the sorting.
        cands = ["xwxexbx", "sub-web-x", "aweb", "webby"]
        assert picker.rank("web", cands) == [3, 1, 2, 0]

    def test_ties_are_broken_by_original_order_and_nothing_else(self):
        cands = ["web-1", "web-2", "web-3"]
        assert picker.rank("web", cands) == [0, 1, 2]
        assert picker.rank("web", list(reversed(cands))) == [0, 1, 2]

    def test_a_shorter_candidate_does_not_jump_the_queue(self):
        # Deliberate: length is NOT a tie-break. Original order is the only one.
        assert picker.rank("web", ["web-application-service", "web"]) == [0, 1]

    def test_non_matches_are_dropped(self):
        assert picker.rank("web", ["web", "nope", "webby"]) == [0, 2]

    def test_an_empty_query_is_the_whole_list_in_order(self):
        assert picker.rank("", ["c", "a", "b"]) == [0, 1, 2]
        assert picker.rank("   ", ["c", "a", "b"]) == [0, 1, 2]

    def test_an_empty_candidate_list_is_empty(self):
        assert picker.rank("web", []) == []

    def test_ranking_is_stable_across_repeated_calls(self):
        cands = ["alpha-web", "web-alpha", "webalpha", "wxexbx"]
        first = picker.rank("web", cands)
        assert all(picker.rank("web", cands) == first for _ in range(5))


class TestFilterModeIsEnteredDeliberately:
    def test_an_empty_query_does_not_filter(self):
        assert not _state(["api", "web"]).filtering

    def test_a_digit_query_does_not_filter(self):
        # Row numbers are how the menu has always been driven; typing "12"
        # must stay a row address, not a search for the string "12".
        st = _state(["api", "web"])
        _keys(st, "1", "2")
        assert st.query == "12"
        assert not st.filtering
        assert st.visible() == [0, 1]

    def test_a_reserved_command_does_not_filter(self):
        # ...so `q` is still Quit even with a project called `queue-worker`.
        st = _state(["queue-worker"], commands=frozenset({"q"}))
        _keys(st, "q")
        assert not st.filtering
        assert st.visible() == [0]

    def test_a_letter_enters_filter_mode(self):
        st = _state(["api", "web"])
        _keys(st, "w")
        assert st.filtering
        assert st.visible() == [1]

    def test_a_command_letter_plus_more_is_a_filter_again(self):
        st = _state(["queue-worker"], commands=frozenset({"q"}))
        _keys(st, "q", "u")
        assert st.filtering
        assert st.visible() == [0]


class TestEnterKeepsTheOldContract:
    def test_bare_enter_yields_the_empty_text_the_caller_defaults_from(self):
        got = _keys(_state(["api", "web"]), picker.ENTER)
        assert got == picker.PickerResult(picker.TEXT, "")

    def test_digits_then_enter_yield_that_text(self):
        got = _keys(_state(["api", "web"]), "1", "2", picker.ENTER)
        assert got == picker.PickerResult(picker.TEXT, "12")

    def test_a_command_then_enter_yields_that_text(self):
        got = _keys(
            _state(["queue-worker"], commands=frozenset({"q"})), "q", picker.ENTER
        )
        assert got == picker.PickerResult(picker.TEXT, "q")

    def test_a_filter_then_enter_selects_the_best_match(self):
        got = _keys(_state(["api", "web-app", "webby"]), "w", "e", picker.ENTER)
        assert got == picker.PickerResult(picker.SELECT, "2")

    def test_enter_with_no_matches_falls_back_to_text(self):
        st = _state(["api", "web"])
        got = _keys(st, "z", "z", picker.ENTER)
        assert got == picker.PickerResult(picker.TEXT, "zz")
        assert st.visible() == []


class TestArrowNavigation:
    def test_down_then_enter_takes_the_second_row(self):
        got = _keys(_state(["api", "web", "docs"]), picker.DOWN, picker.ENTER)
        assert got == picker.PickerResult(picker.SELECT, "2")

    def test_up_from_the_top_wraps_to_the_bottom(self):
        got = _keys(_state(["api", "web", "docs"]), picker.UP, picker.ENTER)
        assert got == picker.PickerResult(picker.SELECT, "3")

    def test_down_past_the_end_wraps_to_the_top(self):
        got = _keys(
            _state(["api", "web"]), picker.DOWN, picker.DOWN, picker.DOWN, picker.ENTER
        )
        assert got == picker.PickerResult(picker.SELECT, "2")

    def test_arrows_move_within_the_filtered_view_only(self):
        st = _state(["api", "web-app", "docs", "webby"])
        got = _keys(st, "w", "e", "b", picker.DOWN, picker.ENTER)
        assert got == picker.PickerResult(picker.SELECT, "4")

    def test_arrows_on_an_empty_filtered_view_do_nothing(self):
        st = _state(["api"])
        _keys(st, "z", picker.DOWN)
        assert st.highlighted() is None
        assert not st.moved

    def test_typing_after_an_arrow_re_aims_at_the_new_best_match(self):
        st = _state(["api", "web-app", "webby"])
        got = _keys(st, picker.DOWN, picker.DOWN, "w", "e", "b", picker.ENTER)
        assert got == picker.PickerResult(picker.SELECT, "2")


class TestEscapeAndBackspace:
    def test_backspace_shortens_the_query(self):
        st = _state(["api", "web"])
        _keys(st, "w", "e", picker.BACKSPACE)
        assert st.query == "w"

    def test_backspace_on_an_empty_query_is_harmless(self):
        st = _state(["api"])
        _keys(st, picker.BACKSPACE)
        assert st.query == ""

    def test_backspacing_back_to_empty_restores_the_default_enter(self):
        st = _state(["api", "web"])
        got = _keys(st, "w", picker.BACKSPACE, picker.ENTER)
        assert got == picker.PickerResult(picker.TEXT, "")

    def test_escape_clears_a_query_without_committing(self):
        st = _state(["api", "web"])
        assert _keys(st, "w", "e", picker.ESC) is None
        assert st.query == "" and not st.filtering

    def test_escape_on_an_empty_query_cancels(self):
        got = _keys(_state(["api"]), picker.ESC)
        assert got == picker.PickerResult(picker.CANCEL, "")

    def test_escape_also_forgets_arrow_movement(self):
        st = _state(["api", "web"])
        _keys(st, "w", picker.DOWN, picker.ESC)
        got = _keys(st, picker.ENTER)
        assert got == picker.PickerResult(picker.TEXT, "")


class TestItemKeysAreStableUnderFiltering:
    def test_a_filtered_row_keeps_its_printed_number(self):
        # The number next to a row is its identity, so a filtered list is still
        # addressable by the digits the user can see.
        st = _state(["api", "web", "docs"])
        _keys(st, "d", "o")
        assert [st.items[i].key for i in st.visible()] == ["3"]

    def test_the_haystack_can_differ_from_the_label(self):
        st = picker.PickerState(
            [
                picker.PickerItem(key="1", label="Open the widget service"),
                picker.PickerItem(key="2", label="Something else", haystack="widget"),
            ]
        )
        _keys(st, "w", "i", "d")
        # Item 2's haystack is a prefix match; item 1's label is a boundary one.
        assert st.visible() == [1, 0]


class TestKeyDecoding:
    @pytest.mark.parametrize(
        ("char", "token"),
        [
            ("\r", picker.ENTER),
            ("\n", picker.ENTER),
            ("\x7f", picker.BACKSPACE),
            ("\x08", picker.BACKSPACE),
            ("\x1b", picker.ESC),
            ("\x10", picker.UP),
            ("\x0e", picker.DOWN),
            ("a", "a"),
            ("Z", "Z"),
            ("-", "-"),
            (" ", " "),
            ("\t", picker.IGNORED),
            ("\x00", picker.IGNORED),
        ],
    )
    def test_plain_characters_map_to_tokens(self, char, token):
        assert picker._classify(char) == token

    def test_ctrl_c_raises_keyboard_interrupt(self):
        with pytest.raises(KeyboardInterrupt):
            picker._classify("\x03")

    def test_windows_arrow_prefixes_decode_to_arrows(self):
        assert picker._WIN_SPECIAL["H"] == picker.UP
        assert picker._WIN_SPECIAL["P"] == picker.DOWN

    def test_posix_csi_finals_decode_to_arrows(self):
        assert picker._CSI_SPECIAL["A"] == picker.UP
        assert picker._CSI_SPECIAL["B"] == picker.DOWN

    def test_an_ignored_key_changes_nothing(self):
        st = _state(["api"])
        assert st.press(picker.IGNORED) is None
        assert st.query == "" and not st.moved


class TestRawModeGate:
    def test_a_non_tty_stdin_is_never_raw(self, monkeypatch):
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: False, raising=False)
        assert picker.raw_mode_available() is False

    def test_a_stdin_that_cannot_answer_isatty_is_never_raw(self, monkeypatch):
        class _Broken:
            def isatty(self):
                raise ValueError("detached")

        monkeypatch.setattr(picker.sys, "stdin", _Broken())
        assert picker.raw_mode_available() is False

    def test_a_real_tty_with_the_os_module_present_is_raw(self, monkeypatch):
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True, raising=False)
        assert picker.raw_mode_available() is True


class TestPainting:
    def _paint(self, st, **kw):
        import click.testing

        runner = click.testing.CliRunner()
        with runner.isolation() as out:
            picker.paint(st, lambda: None, **kw)
            raw = out[0].getvalue().decode("utf-8", "replace")
        return raw.replace("\r\n", "\n")

    def test_the_unfiltered_paint_carries_every_row_unmarked(self):
        text = self._paint(_state(["api", "web"]))
        assert "api" in text and "web" in text
        assert ">" not in text

    def test_a_filter_marks_the_best_match_and_drops_the_rest(self):
        st = _state(["api", "web"])
        _keys(st, "w")
        text = self._paint(st)
        assert "> 2   web" in text
        assert "api" not in text

    def test_an_arrow_marks_the_row_even_with_no_query(self):
        st = _state(["api", "web"])
        _keys(st, picker.DOWN)
        text = self._paint(st)
        assert "> 2   web" in text
        assert "api" in text  # nothing is filtered out

    def test_no_match_says_so(self):
        st = _state(["api"])
        _keys(st, "z", "z")
        assert "no match for" in self._paint(st)

    def test_gaps_render_only_in_the_unfiltered_view(self):
        st = picker.PickerState(
            [
                picker.PickerItem(key="1", label="first"),
                picker.PickerItem(key="q", label="quaxly", gap_before=True),
            ]
        )
        assert "\n\n" in self._paint(st)
        _keys(st, "q", "u")
        assert "\n\n" not in self._paint(st).rstrip("\n")

    def test_the_prompt_echoes_the_query(self):
        st = _state(["api"])
        _keys(st, "a", "p")
        assert "  > ap" in self._paint(st, prompt="  > ")
