"""`magent terminal icons`, the icon half of `magent terminal status`, and the
doctor `wt-icons` row. Every fragment lives in the tmp root the conftest guard
redirects; a developer's real Windows Terminal fragments are never touched."""

from __future__ import annotations

import json

import pytest

from magent import cli, wt_profiles
from magent.cli import doctor
from magent.config import MagentConfig, Settings
from tests.conftest import FakePlatform


@pytest.fixture
def icons_on(monkeypatch):
    monkeypatch.setenv("MAGENT_WT_ICONS", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


@pytest.fixture
def on_wt(monkeypatch):
    monkeypatch.setattr(
        "magent.platform.get_platform",
        lambda: FakePlatform(supports_wt_profiles=True, supports_wt_keybindings=True),
    )


def _seed(*keys: str) -> None:
    wt_profiles.sync([wt_profiles.IconSpec(k, k, "#336699") for k in keys])


def _cfg_file(tmp_path, **settings) -> str:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps({"version": 4, "projects": [], "settings": settings}),
        encoding="utf-8",
    )
    return str(path)


class TestIconsCommand:
    def test_unsupported_platform_says_so(self, runner, monkeypatch):
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        result = runner.invoke(cli.main, ["terminal", "icons"])
        assert result.exit_code == 0
        assert "Windows Terminal feature" in result.stdout

    def test_lists_the_profiles_and_where_the_icon_came_from(
        self, runner, on_wt, icons_on
    ):
        _seed("api", "web")
        result = runner.invoke(cli.main, ["terminal", "icons"])
        assert result.exit_code == 0
        assert "magent: api" in result.stdout
        assert "magent: web" in result.stdout
        assert "generated" in result.stdout
        assert "1.24" in result.stdout

    def test_empty_fragment_explains_when_it_appears(self, runner, on_wt, icons_on):
        result = runner.invoke(cli.main, ["terminal", "icons"])
        assert "No profiles yet" in result.stdout

    def test_a_missing_icon_file_is_flagged(self, runner, on_wt, icons_on):
        _seed("api")
        for icon in wt_profiles.fragment_dir().glob("*.png"):
            icon.unlink()
        result = runner.invoke(cli.main, ["terminal", "icons"])
        assert "icon file missing" in result.stdout

    def test_reports_the_kill_switch(self, runner, on_wt):
        # conftest pins MAGENT_WT_ICONS=0.
        result = runner.invoke(cli.main, ["terminal", "icons"])
        assert "off (MAGENT_WT_ICONS=0)" in result.stdout

    def test_reports_the_setting(self, runner, on_wt, icons_on, tmp_path):
        cfg = _cfg_file(tmp_path, terminalIcons=False)
        result = runner.invoke(cli.main, ["--config", cfg, "terminal", "icons"])
        assert "off (settings.terminalIcons is false)" in result.stdout

    def test_remove_deletes_the_fragment_and_only_that(
        self, runner, on_wt, icons_on, tmp_path
    ):
        _seed("api")
        sibling = wt_profiles.fragments_root() / "someone-else"
        sibling.mkdir()
        (sibling / "theirs.json").write_text("{}", encoding="utf-8")
        result = runner.invoke(cli.main, ["terminal", "icons", "--remove"])
        assert result.exit_code == 0
        assert "Removed" in result.stdout
        assert not wt_profiles.fragment_dir().exists()
        assert (sibling / "theirs.json").is_file()

    def test_remove_leaves_a_folder_that_holds_someone_elses_file(
        self, runner, on_wt, icons_on
    ):
        _seed("api")
        directory = wt_profiles.fragment_dir()
        (directory / "notes.txt").write_text("mine", encoding="utf-8")
        result = runner.invoke(cli.main, ["terminal", "icons", "--remove"])
        assert result.exit_code == 0
        assert "Removed" not in result.stdout
        assert "Kept" in result.stdout
        assert (directory / "notes.txt").is_file()
        assert not (directory / wt_profiles.FRAGMENT_FILE).exists()

    def test_remove_with_nothing_there(self, runner, on_wt, icons_on):
        result = runner.invoke(cli.main, ["terminal", "icons", "--remove"])
        assert result.exit_code == 0
        assert "Nothing to remove" in result.stdout

    def test_remove_works_under_the_kill_switch(self, runner, on_wt, monkeypatch):
        # An explicit command is not the feature the kill switch silences.
        monkeypatch.setenv("MAGENT_WT_ICONS", "1")
        monkeypatch.setattr("magent.env._cached_env", None)
        _seed("api")
        monkeypatch.setenv("MAGENT_WT_ICONS", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        result = runner.invoke(cli.main, ["terminal", "icons", "--remove"])
        assert "Removed" in result.stdout


class TestStatusShowsTheIcons:
    def test_status_appends_the_icon_section(self, runner, on_wt, icons_on, tmp_path):
        _seed("api")
        settings = tmp_path / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        result = runner.invoke(
            cli.main, ["terminal", "status", "--settings-file", str(settings)]
        )
        assert result.exit_code == 0
        assert "Tab icons" in result.stdout
        assert "magent: api" in result.stdout

    def test_status_without_wt_profiles_is_what_it_was(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "magent.platform.get_platform",
            lambda: FakePlatform(supports_wt_keybindings=True),
        )
        settings = tmp_path / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        result = runner.invoke(
            cli.main, ["terminal", "status", "--settings-file", str(settings)]
        )
        assert "Tab icons" not in result.stdout


class TestDoctorRow:
    def _check(self, monkeypatch, cfg=None, *, wt=True):
        monkeypatch.setattr(
            "magent.platform.get_platform",
            lambda: FakePlatform(supports_wt_profiles=wt),
        )
        return doctor._check_wt_icons(cfg)

    def test_not_applicable_off_windows(self, monkeypatch):
        status, detail = self._check(monkeypatch, wt=False)
        assert status == doctor.OK
        assert "not applicable" in detail

    def test_kill_switch_is_ok_and_named(self, monkeypatch):
        status, detail = self._check(monkeypatch)
        assert status == doctor.OK
        assert "MAGENT_WT_ICONS=0" in detail

    def test_setting_off_is_ok_and_named(self, monkeypatch, icons_on):
        cfg = MagentConfig(projects=[], settings=Settings(terminal_icons=False))
        status, detail = self._check(monkeypatch, cfg)
        assert status == doctor.OK
        assert "settings.terminalIcons" in detail

    def test_no_fragment_yet_is_ok(self, monkeypatch, icons_on):
        status, detail = self._check(monkeypatch)
        assert status == doctor.OK
        assert "no tab-icon profiles yet" in detail

    def test_healthy_fragment_counts_its_profiles(self, monkeypatch, icons_on):
        _seed("api", "web")
        status, detail = self._check(monkeypatch)
        assert status == doctor.OK
        assert "2 tab-icon profile(s)" in detail

    def test_a_missing_icon_file_warns_and_never_fails(self, monkeypatch, icons_on):
        _seed("api")
        for icon in wt_profiles.fragment_dir().glob("*.png"):
            icon.unlink()
        status, detail = self._check(monkeypatch)
        assert status == doctor.WARN
        assert "api" in detail

    def test_no_localappdata_warns(self, monkeypatch, icons_on):
        monkeypatch.setattr(wt_profiles, "fragments_root", lambda: None)
        status, _detail = self._check(monkeypatch)
        assert status == doctor.WARN
