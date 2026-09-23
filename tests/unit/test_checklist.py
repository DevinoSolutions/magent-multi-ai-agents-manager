"""The launch checklist: the state machine, the frame, and the tty gate.

Two halves, and the second is the one that protects everything else. The state
machine is a pure function of keystrokes, so every transition is pinned without
a terminal. The GATE is pinned at the CLI: `--go` off a tty (which is every
`CliRunner` invocation, every script and all of CI) must never prompt and must
launch the whole fleet, and `--all` must do the same on a tty. A prompt that
leaks into a non-interactive launch does not fail a test -- it hangs one.
"""

from __future__ import annotations

import pytest

from magent.cli import checklist
from magent.cli.app import main
from magent.cli.checklist import (
    ABORT,
    LAUNCH,
    UNGROUPED,
    ChecklistItem,
    ChecklistState,
    choose_projects,
)
from magent.cli.picker import DOWN, ENTER, ESC, UP
from magent.config import MagentConfig, ProjectConfig


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

    def test_n_deselects_all_and_a_selects_all(self):
        state = _state("alpha", "beta")

        assert state.press("n") is None
        assert state.names() == ()
        assert state.press("a") is None
        assert state.names() == ("alpha", "beta")


class TestNavigation:
    @pytest.mark.parametrize("down", [DOWN, "j"])
    @pytest.mark.parametrize("up", [UP, "k"])
    def test_the_cursor_moves_and_wraps(self, up: str, down: str):
        state = _state("alpha", "beta")

        state.press(down)
        assert state.cursor == 1
        state.press(down)
        assert state.cursor == 0  # wrapped
        state.press(up)
        assert state.cursor == 1  # wrapped the other way

    def test_moving_toggles_nothing(self):
        state = _state("alpha", "beta")

        state.press(DOWN)

        assert state.names() == ("alpha", "beta")


class TestSectionToggle:
    def test_g_unchecks_a_fully_checked_section_leaving_the_others_alone(self):
        state = _grouped()  # cursor on "alpha", in "work"

        assert state.press("g") is None

        assert state.names() == ("gamma",)

    def test_g_checks_a_mixed_section_rather_than_inverting_it(self):
        state = _grouped()
        state.press(" ")  # uncheck alpha -> "work" is now mixed

        state.press("g")

        assert state.names() == ("alpha", "beta", "gamma")

    def test_g_acts_on_the_cursor_rows_own_section(self):
        state = _grouped()
        state.press(DOWN)
        state.press(DOWN)  # cursor on "gamma", in "play"

        state.press("g")

        assert state.names() == ("alpha", "beta")


class TestDigitShortcuts:
    def test_a_digit_toggles_that_numbered_row_and_moves_the_cursor(self):
        state = _state("alpha", "beta", "gamma")

        assert state.press("2") is None

        assert state.names() == ("alpha", "gamma")
        assert state.cursor == 1

    def test_a_digit_past_the_last_row_does_nothing(self):
        state = _state("alpha")

        state.press("7")

        assert state.names() == ("alpha",)
        assert state.cursor == 0


class TestLeaving:
    def test_enter_with_nothing_checked_aborts_rather_than_launching_nothing(self):
        state = _state("alpha", "beta")
        state.press("n")

        result = state.press(ENTER)

        assert result is not None
        assert result.kind == ABORT

    @pytest.mark.parametrize("key", ["q", ESC])
    def test_q_and_esc_abort(self, key: str):
        state = _state("alpha")

        result = state.press(key)

        assert result is not None
        assert result.kind == ABORT
        assert result.names == ()


class TestTheFrame:
    def test_it_shows_boxes_sections_numbers_and_the_hints(self, capsys):
        state = _grouped()
        state.press(" ")  # uncheck the first row so both boxes are on screen

        checklist.paint(state)

        out = capsys.readouterr().out
        assert "[ ] alpha" in out
        assert "[x] beta" in out
        assert "work" in out
        assert "play" in out
        assert " 1  " in out
        assert "2 of 3 selected" in out
        assert checklist.HINT in out

    def test_every_glyph_is_ascii(self, capsys):
        # An ambiguous-width glyph has corrupted a magent status bar before.
        checklist.paint(_grouped())

        capsys.readouterr().out.encode("ascii")


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
