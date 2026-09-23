"""Characterization pins for the interactive main menu's LINE-BASED path.

Written and green BEFORE the type-to-filter picker landed, per the house rule.
Everything here drives `_show_menu` the way a pipe / `CliRunner` / a script
does -- stdin is not a terminal -- because that path is a hard backwards-
compatibility constraint: the raw-key picker is gated on `sys.stdin.isatty()`
and must leave this one byte-for-byte alone.

The pins are the menu's whole answer vocabulary: every single-key command, the
default that a bare Enter takes, the group submenu's index pick, and the
invalid-choice redraw.
"""

from __future__ import annotations

import sys

import pytest

from magent.cli import menu as menu_mod


@pytest.fixture
def keys(monkeypatch):
    """Feed `_show_menu` a scripted sequence of typed lines."""

    def _feed(*answers: str) -> list[str]:
        seq = iter(answers)
        asked: list[str] = []

        def _prompt(text, **kwargs):
            asked.append(text)
            return next(seq)

        monkeypatch.setattr(menu_mod.click, "prompt", _prompt)
        # A non-tty stdin is the whole point of these pins.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        return asked

    return _feed


class TestSingleKeyCommandsAreUnchanged:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("1", {"action": "run", "retile_all": False, "group": None}),
            ("2", {"action": "run", "retile_all": True, "group": None}),
            ("u", {"action": "up"}),
            ("s", {"action": "sessions"}),
            ("a", {"action": "attach"}),
            ("t", {"action": "status"}),
            ("d", {"action": "down"}),
            ("q", {"action": "quit"}),
        ],
    )
    def test_each_key_returns_its_action(self, keys, typed, expected):
        keys(typed)
        got = menu_mod._show_menu([])
        for k, v in expected.items():
            assert got[k] == v

    def test_uppercase_and_surrounding_space_still_work(self, keys):
        keys("  Q  ")
        assert menu_mod._show_menu([])["action"] == "quit"


class TestTheDefaultIsStillOptionOne:
    def test_the_prompt_declares_1_as_its_default(self, monkeypatch):
        seen: list[object] = []

        def _prompt(text, **kwargs):
            seen.append(kwargs.get("default"))
            return "q"

        monkeypatch.setattr(menu_mod.click, "prompt", _prompt)
        menu_mod._show_menu([])
        assert seen == ["1"]


class TestGroupSubmenu:
    def test_group_option_is_hidden_without_groups(self, keys, capsys):
        keys("q")
        menu_mod._show_menu([])
        assert "Launch a group" not in capsys.readouterr().out

    def test_index_pick_returns_that_group(self, keys):
        keys("3", "2")
        assert menu_mod._show_menu(["alpha", "beta"])["group"] == "beta"

    def test_out_of_range_pick_falls_back_to_the_menu(self, keys, capsys):
        keys("3", "99", "q")
        assert menu_mod._show_menu(["alpha"])["action"] == "quit"
        assert "Invalid choice" in capsys.readouterr().out

    def test_non_numeric_pick_falls_back_to_the_menu(self, keys, capsys):
        keys("3", "zzz", "q")
        assert menu_mod._show_menu(["alpha"])["action"] == "quit"
        assert "Invalid choice" in capsys.readouterr().out


class TestUnknownInput:
    def test_unknown_key_redraws_and_reprompts(self, keys, capsys):
        keys("zzz", "q")
        assert menu_mod._show_menu([])["action"] == "quit"
        assert "Invalid choice" in capsys.readouterr().out
