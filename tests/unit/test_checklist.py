"""The launch checklist: the state machine, the frame, and the tty gate.

Two halves, and the second is the one that protects everything else. The state
machine is a pure function of keystrokes, so every transition is pinned without
a terminal. The GATE is pinned at the CLI: `--go` off a tty (which is every
`CliRunner` invocation, every script and all of CI) must never prompt and must
launch the whole fleet, and `--all` must do the same on a tty. A prompt that
leaks into a non-interactive launch does not fail a test -- it hangs one.
"""

from __future__ import annotations

import contextlib

import click
import pytest

from magent.cli import checklist, picker
from magent.cli.app import main
from magent.cli.checklist import (
    ABORT,
    LAUNCH,
    UNGROUPED,
    ChecklistItem,
    ChecklistState,
    choose_projects,
)
from magent.cli.picker import (
    BACKSPACE,
    BTAB,
    CTRL_A,
    DOWN,
    END,
    ENTER,
    ESC,
    HOME,
    PGDN,
    PGUP,
    TAB,
    UP,
)
from magent.config import MagentConfig, ProjectConfig


@contextlib.contextmanager
def _null_session():
    yield


def _state(*names: str) -> ChecklistState:
    return ChecklistState([ChecklistItem(n) for n in names])


def _grouped() -> ChecklistState:
    return ChecklistState(
        [
            ChecklistItem("alpha", "work"),
            ChecklistItem("beta", "work"),
            ChecklistItem("gamma", "play"),
        ]
    )


class TestEverythingStartsChecked:
    def test_enter_on_an_untouched_list_takes_them_all(self):
        # The whole point of the default: Enter is byte-for-byte today's --go.
        state = _state("alpha", "beta", "gamma")

        result = state.press(ENTER)

        assert result is not None
        assert result.kind == LAUNCH
        assert result.names == ("alpha", "beta", "gamma")


def _type(state: ChecklistState, text: str) -> None:
    for ch in text:
        assert state.press(ch) is None


def _fleet(count: int = 30) -> ChecklistState:
    """`count` projects in three sections, named so a query can pick them out."""
    groups = ["work", "play", "ops"]
    return ChecklistState(
        [ChecklistItem(f"proj-{i:02d}", groups[i * 3 // count]) for i in range(count)]
    )


class TestToggling:
    def test_space_unchecks_the_cursor_row_then_rechecks_it(self):
        state = _state("alpha", "beta")

        assert state.press(" ") is None
        assert state.names() == ("beta",)
        assert state.press(" ") is None
        assert state.names() == ("alpha", "beta")

    def test_names_come_back_in_row_order_not_set_order(self):
        state = _state("alpha", "beta", "gamma")
        state.checked.clear()
        state.checked.update({2, 0})

        assert state.names() == ("alpha", "gamma")


class TestNavigation:
    def test_the_cursor_moves_and_wraps(self):
        state = _state("alpha", "beta")

        state.press(DOWN)
        assert state.cursor == 1
        state.press(DOWN)
        assert state.cursor == 0  # wrapped
        state.press(UP)
        assert state.cursor == 1  # wrapped the other way

    def test_moving_toggles_nothing(self):
        state = _state("alpha", "beta")

        state.press(DOWN)

        assert state.names() == ("alpha", "beta")

    def test_home_end_and_pages_clamp_instead_of_wrapping(self):
        state = _fleet(30)
        state.page = 8

        state.press(END)
        assert state.cursor == 29
        state.press(PGDN)
        assert state.cursor == 29
        state.press(PGUP)
        assert state.cursor == 21
        state.press(HOME)
        assert state.cursor == 0
        state.press(PGUP)
        assert state.cursor == 0

    def test_letters_are_filter_text_not_commands(self):
        # j/k/q/a/n/g used to be commands; with a filter they are just letters.
        state = _state("jack", "kilo", "queue", "alpha")

        _type(state, "q")

        assert state.query == "q"
        assert state.visible() == [2]


class TestFilter:
    def test_typing_narrows_with_the_pickers_own_ranking(self):
        state = _state("my-dis", "dispatch", "radio-dis", "xdxixsx")

        _type(state, "dis")

        # prefix, then the two boundary matches in config order, then the
        # subsequence.
        assert [state.items[i].name for i in state.visible()] == [
            "dispatch",
            "my-dis",
            "radio-dis",
            "xdxixsx",
        ]

    def test_it_matches_what_the_single_select_picker_would_show(self):
        names = ["api-gateway", "gateway", "agate", "unrelated", "web-gate"]
        state = _state(*names)

        _type(state, "gate")

        assert state.visible() == picker.rank("gate", names)

    def test_the_cursor_starts_on_the_best_match_after_every_keystroke(self):
        state = _state("zeta", "alpha", "beta")
        state.press(DOWN)
        state.press(DOWN)

        _type(state, "b")

        assert state.cursor == 0
        assert state.highlighted() == 2

    def test_backspace_edits_and_an_empty_query_restores_the_full_list(self):
        state = _state("alpha", "beta", "gamma")
        _type(state, "be")
        assert state.visible() == [1]

        state.press(BACKSPACE)
        state.press(BACKSPACE)

        assert state.query == ""
        assert state.visible() == [0, 1, 2]
        state.press(BACKSPACE)  # on an empty query: nothing to erase
        assert state.query == ""

    @pytest.mark.parametrize("key", ["-", "_", ".", "7", "X"])
    def test_name_characters_go_into_the_query(self, key: str):
        state = _state("alpha")

        state.press(key)

        assert state.query == key

    @pytest.mark.parametrize("key", ["!", "/", "tab", "pgup", "ctrl-a", ""])
    def test_other_keys_never_leak_into_the_query(self, key: str):
        state = _state("alpha")

        state.press(key)

        assert state.query == ""

    def test_a_query_matching_nothing_leaves_an_empty_list(self):
        state = _state("alpha")

        _type(state, "zzz")

        assert state.visible() == []
        assert state.highlighted() is None
        assert state.press(ENTER) is None  # nothing to launch, still typing
        state.press(DOWN)  # navigation on an empty list is harmless
        assert state.cursor == 0


class TestSelectionSurvivesTheFilter:
    def test_a_checked_row_stays_checked_while_hidden_and_after_the_filter_clears(
        self,
    ):
        state = _state("alpha", "beta", "gamma")
        state.checked.clear()
        state.pristine = False
        state.press(" ")  # check alpha
        _type(state, "gam")
        state.press(" ")  # check gamma through the filter

        state.press(ESC)  # clear the filter

        assert state.names() == ("alpha", "gamma")

    def test_selection_is_by_project_not_by_row_position(self):
        state = _state("alpha", "beta", "gamma")
        state.checked.clear()
        state.pristine = False
        _type(state, "gamma")
        state.press(" ")  # position 0 of the filtered list is item 2

        assert state.checked == {2}

    def test_the_first_typed_character_clears_the_untouched_all_checked_default(
        self,
    ):
        # Otherwise "type a name, press Enter" launches every project hiding
        # behind the filter.
        state = _state("alpha", "beta", "gamma")

        _type(state, "bet")
        result = state.press(ENTER)

        assert result is not None
        assert result.names == ("beta",)

    def test_a_hand_made_selection_is_not_wiped_by_typing(self):
        state = _state("alpha", "beta", "gamma")
        state.press(" ")  # uncheck alpha: the selection is now the user's own

        _type(state, "gam")

        assert state.names() == ("beta", "gamma")


class TestCtrlA:
    def test_it_checks_only_the_visible_rows(self):
        state = _state("alpha", "beta", "alpine", "gamma")
        state.checked.clear()
        state.pristine = False
        _type(state, "alp")

        state.press(CTRL_A)

        assert state.names() == ("alpha", "alpine")

    def test_pressed_again_it_clears_only_the_visible_rows(self):
        state = _state("alpha", "beta", "alpine")
        state.pristine = False  # everything checked, by the user's own hand
        _type(state, "alp")

        state.press(CTRL_A)  # all visible rows checked -> clears them

        assert state.names() == ("beta",)

    def test_with_a_mixed_visible_set_it_checks_the_rest(self):
        state = _state("alpha", "beta")
        state.press(" ")  # uncheck alpha

        state.press(CTRL_A)

        assert state.names() == ("alpha", "beta")


class TestGroupJumps:
    def test_tab_goes_to_the_next_sections_first_row_and_wraps(self):
        state = _grouped()  # work: 0-1, play: 2

        state.press(TAB)
        assert state.cursor == 2
        state.press(TAB)
        assert state.cursor == 0  # wrapped

    def test_shift_tab_goes_to_the_previous_sections_first_row(self):
        state = _grouped()
        state.press(END)  # gamma, the only row of "play"

        state.press(BTAB)
        assert state.cursor == 0  # previous section's first row... of "work"

        state.press(BTAB)
        assert state.cursor == 2  # wrapped to the last section

    def test_shift_tab_from_mid_section_lands_on_that_sections_start(self):
        state = ChecklistState(
            [
                ChecklistItem("a", "one"),
                ChecklistItem("b", "one"),
                ChecklistItem("c", "one"),
                ChecklistItem("d", "two"),
            ]
        )
        state.cursor = 2

        state.press(BTAB)

        assert state.cursor == 0

    def test_there_are_no_sections_to_jump_between_under_a_filter(self):
        state = _grouped()
        _type(state, "a")  # alpha, beta, gamma all match
        before = state.cursor

        state.press(TAB)

        assert state.cursor == before


class TestLeaving:
    def test_enter_launches_the_checked_set(self):
        state = _state("alpha", "beta", "gamma")
        state.press(" ")  # uncheck alpha

        result = state.press(ENTER)

        assert result is not None
        assert (result.kind, result.names) == (LAUNCH, ("beta", "gamma"))

    def test_enter_with_nothing_checked_launches_just_the_highlighted_project(self):
        state = _state("alpha", "beta", "gamma")
        state.checked.clear()
        state.pristine = False
        state.press(DOWN)

        result = state.press(ENTER)

        assert result is not None
        assert (result.kind, result.names) == (LAUNCH, ("beta",))

    def test_esc_cancels_on_an_empty_query(self):
        state = _state("alpha")

        result = state.press(ESC)

        assert result is not None
        assert (result.kind, result.names) == (ABORT, ())

    def test_esc_clears_a_query_before_it_cancels(self):
        state = _state("alpha", "beta")
        _type(state, "bet")

        assert state.press(ESC) is None
        assert state.query == ""
        assert state.visible() == [0, 1]
        result = state.press(ESC)
        assert result is not None
        assert result.kind == ABORT

    def test_q_is_a_letter_now_not_a_cancel(self):
        state = _state("queue")

        assert state.press("q") is None
        assert state.query == "q"


class TestViewportMath:
    """`scroll_top` / `hidden_rows` / `frame`: the cursor's row is ALWAYS inside
    the window and the counters say exactly what is cut off."""

    @pytest.mark.parametrize("height", [3, 4, 5, 8, 17])
    def test_the_cursor_row_is_visible_at_every_position(self, height: int):
        state = _fleet(30)
        for cursor in range(30):
            state.cursor = cursor
            lines = checklist.list_lines(state)

            state.top = checklist.scroll_top(lines, cursor, height, state.top)

            window = lines[state.top : state.top + height]
            assert any(line.pos == cursor for line in window), (height, cursor)

    @pytest.mark.parametrize("rows", [10, 12, 20, 30])
    def test_a_frame_never_outgrows_the_terminal(self, rows: int):
        state = _fleet(30)
        for cursor in (0, 7, 15, 29):
            state.cursor = cursor

            lines = checklist.frame(state, rows, 80)

            assert len(lines) <= rows - 1

    def test_the_counters_add_up_to_every_row(self):
        state = _fleet(30)
        state.cursor = 12
        height = checklist.body_height(20)
        lines = checklist.list_lines(state)
        state.top = checklist.scroll_top(lines, 12, height, 0)

        above, below = checklist.hidden_rows(lines, state.top, height)
        shown = sum(
            1 for line in lines[state.top : state.top + height] if line.pos is not None
        )

        assert above + shown + below == 30
        assert above > 0
        assert below > 0

    def test_a_list_that_fits_has_no_counters(self):
        state = _state("alpha", "beta")

        out = [click.unstyle(line) for line in checklist.frame(state, 24, 80)]

        assert not any("more" in line for line in out)

    def test_the_frame_says_how_many_rows_are_cut_off_each_way(self):
        state = _fleet(30)
        state.cursor = 15

        out = [click.unstyle(line) for line in checklist.frame(state, 20, 80)]

        assert any(line.strip().startswith("^ ") and "more" in line for line in out)
        assert any(line.strip().startswith("v ") and "more" in line for line in out)

    def test_the_first_row_of_a_section_brings_its_header_into_view(self):
        state = _fleet(30)
        state.press(TAB)  # first row of the second section
        lines = checklist.list_lines(state)

        top = checklist.scroll_top(lines, state.cursor, 5, 0)

        at = next(n for n, line in enumerate(lines) if line.pos == state.cursor)
        assert lines[at - 1].pos is None  # the header sits right above the row
        assert top <= at - 1

    def test_headers_are_dropped_while_filtering(self):
        state = _fleet(30)
        _type(state, "proj-1")

        assert all(line.pos is not None for line in checklist.list_lines(state))

    def test_tiny_terminals_still_get_a_usable_body(self):
        assert checklist.body_height(3) == 3
        assert checklist.body_height(24) == 17


class TestTheFrame:
    def test_it_shows_boxes_sections_the_filter_line_and_the_footer(self):
        state = _grouped()
        state.press(" ")  # uncheck the first row so both boxes are on screen

        text = "\n".join(click.unstyle(line) for line in checklist.frame(state, 24, 80))

        assert "[ ] alpha" in text
        assert "[x] beta" in text
        assert "work" in text
        assert "play" in text
        assert "filter: _" in text
        assert "2 of 3 selected - type to filter" in text
        assert "space toggle" in text
        assert "ctrl+a all" in text
        assert "tab next group" in text
        assert "esc cancel" in text

    def test_the_footer_names_the_project_enter_will_launch_when_nothing_is_checked(
        self,
    ):
        state = _state("alpha", "beta")
        state.checked.clear()
        state.press(DOWN)

        text = "\n".join(click.unstyle(line) for line in checklist.frame(state, 24, 80))

        assert "enter launch beta" in text

    def test_a_filtered_frame_shows_the_query_the_count_and_each_rows_section(self):
        state = _grouped()
        _type(state, "gam")

        text = "\n".join(click.unstyle(line) for line in checklist.frame(state, 24, 80))

        assert "filter: gam_" in text
        assert "1 shown" in text
        assert "gamma  play" in text
        assert "esc clear" in text
        assert "alpha" not in text

    def test_no_match_says_so(self):
        state = _state("alpha")
        _type(state, "zz")

        text = "\n".join(click.unstyle(line) for line in checklist.frame(state, 24, 80))

        assert "no match for zz" in text

    def test_every_glyph_is_ascii(self, capsys):
        state = _fleet(30)
        state.cursor = 15
        checklist.paint(state)
        _type(state, "pro")
        checklist.paint(state)

        capsys.readouterr().out.encode("ascii")

    def test_a_long_name_is_clipped_to_the_terminal_width(self):
        state = _state("x" * 200)

        lines = [click.unstyle(line) for line in checklist.frame(state, 24, 40)]

        assert max(len(line) for line in lines) <= 40


class TestPaintIsInPlace:
    def test_a_repaint_homes_the_cursor_and_never_ends_in_a_newline(self, monkeypatch):
        written: list[str] = []
        monkeypatch.setattr(
            checklist.click, "echo", lambda msg, **kw: written.append(f"{msg}|{kw}")
        )
        monkeypatch.setattr(checklist, "terminal_size", lambda: (20, 80))

        checklist.paint(_fleet(30))

        out = written[0]
        assert out.startswith("\x1b[H")
        assert "\x1b[J" in out
        assert "'nl': False" in out
        # at most rows-1 lines: the terminal can never scroll
        assert out.count("\n") <= 18

    def test_the_terminal_is_cleared_once_up_front_not_per_frame(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(checklist.click, "clear", lambda: calls.append("clear"))
        monkeypatch.setattr(checklist, "paint", lambda _s: calls.append("paint"))
        monkeypatch.setattr(checklist.picker, "key_session", _null_session)
        keys = iter([DOWN, ENTER])
        monkeypatch.setattr(checklist.picker, "read_key", lambda: next(keys))

        result = checklist.run(_state("alpha", "beta"))

        assert result.kind == LAUNCH
        assert calls == ["clear", "paint", "paint"]


class TestRowOrder:
    def test_groups_come_in_config_order_with_the_ungrouped_last(self):
        items = checklist.items_for(
            [
                ProjectConfig(path="/z", group="zeta"),
                ProjectConfig(path="/loose"),
                ProjectConfig(path="/a", group="alpha"),
                ProjectConfig(path="/z2", group="zeta"),
            ]
        )

        assert [(i.name, i.group) for i in items] == [
            ("z", "zeta"),
            ("z2", "zeta"),
            ("a", "alpha"),
            ("loose", UNGROUPED),
        ]

    def test_the_row_name_is_the_title_when_there_is_one(self):
        items = checklist.items_for([ProjectConfig(path="/gamma", title="renamed")])

        assert [i.name for i in items] == ["renamed"]


class TestTheTtyGate:
    def test_off_a_terminal_nothing_is_asked_and_nothing_is_narrowed(self, monkeypatch):
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: False)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted off a tty")
        )
        cfg = MagentConfig(projects=[ProjectConfig(path="/alpha")])

        choice = choose_projects(cfg)

        assert choice.only is None
        assert not choice.aborted

    def test_off_a_terminal_not_one_byte_is_written_and_no_key_is_read(
        self, monkeypatch, capsys
    ):
        # The navigation rework must not leak a frame, a clear-screen or a key
        # read into the non-interactive path (a raw read there hangs a script).
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: False)
        monkeypatch.setattr(
            checklist.picker, "read_key", lambda: pytest.fail("read a key off a tty")
        )
        monkeypatch.setattr(
            checklist.picker,
            "key_session",
            lambda: pytest.fail("opened a key session off a tty"),
        )
        cfg = MagentConfig(
            projects=[ProjectConfig(path="/alpha"), ProjectConfig(path="/beta")]
        )

        choice = choose_projects(cfg)

        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert choice == checklist.Choice()

    def test_a_group_that_narrows_to_nothing_is_not_asked_about(self, monkeypatch):
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted with no rows")
        )
        cfg = MagentConfig(projects=[ProjectConfig(path="/alpha", group="a")])

        assert choose_projects(cfg, group="nope").only is None

    def test_on_a_terminal_the_checked_names_come_back(self, monkeypatch):
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist,
            "run",
            lambda state: checklist.ChecklistResult(LAUNCH, (state.items[0].name,)),
        )
        cfg = MagentConfig(
            projects=[ProjectConfig(path="/alpha"), ProjectConfig(path="/beta")]
        )

        assert choose_projects(cfg).only == frozenset({"alpha"})

    def test_an_abort_is_reported_as_an_abort_not_an_empty_set(self, monkeypatch):
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: checklist.ChecklistResult(ABORT)
        )
        cfg = MagentConfig(projects=[ProjectConfig(path="/alpha")])

        choice = choose_projects(cfg)

        assert choice.aborted
        assert choice.only is None

    def test_disabled_projects_are_never_offered(self, monkeypatch):
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        seen: list[list[str]] = []

        def _record(state: ChecklistState) -> checklist.ChecklistResult:
            seen.append([i.name for i in state.items])
            return checklist.ChecklistResult(ABORT)

        monkeypatch.setattr(checklist, "run", _record)
        cfg = MagentConfig(
            projects=[
                ProjectConfig(path="/alpha"),
                ProjectConfig(path="/beta", enabled=False),
            ]
        )

        choose_projects(cfg)

        assert seen == [["alpha"]]


class TestTheCliNeverPromptsNonInteractively:
    """`CliRunner` has no tty, which is exactly the condition every script and
    CI job launches under."""

    def _opts(self, monkeypatch):
        import magent.launch as launch_module

        captured: list[launch_module.RunOpts] = []

        def _fake_run(_cfg, opts):
            captured.append(opts)
            return 0

        monkeypatch.setattr(launch_module, "run_magent", _fake_run)
        return captured

    def test_go_off_a_tty_launches_everything_without_a_prompt(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        captured = self._opts(monkeypatch)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted off a tty")
        )
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        cfgpath = tmp_config({"projects": [{"path": str(project_dir)}]})

        result = runner.invoke(main, ["--config", cfgpath, "--go"])

        assert result.exit_code == 0
        assert [o.only for o in captured] == [None]

    def test_all_skips_the_checklist_even_on_a_terminal(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        captured = self._opts(monkeypatch)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted under --all")
        )
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        cfgpath = tmp_config({"projects": [{"path": str(project_dir)}]})

        result = runner.invoke(main, ["--config", cfgpath, "--go", "--all"])

        assert result.exit_code == 0
        assert [o.only for o in captured] == [None]

    def test_an_abort_launches_nothing_and_exits_zero(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        captured = self._opts(monkeypatch)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: checklist.ChecklistResult(ABORT)
        )
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        cfgpath = tmp_config({"projects": [{"path": str(project_dir)}]})

        result = runner.invoke(main, ["--config", cfgpath, "--go"])

        assert result.exit_code == 0
        assert captured == []
        assert checklist.ABORT_MESSAGE in result.stdout

    def test_a_pure_retile_is_never_asked_what_to_launch(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        captured = self._opts(monkeypatch)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted for a retile")
        )
        project_dir = tmp_path / "myapp"
        project_dir.mkdir()
        cfgpath = tmp_config({"projects": [{"path": str(project_dir)}]})

        result = runner.invoke(main, ["--config", cfgpath, "--retile-all"])

        assert result.exit_code == 0
        assert [o.tile_only for o in captured] == [True]
        assert [o.only for o in captured] == [None]
