"""wt_profiles: the magent-owned Windows Terminal fragment that gives every tab
a real image icon. Everything runs against the tmp fragments root the autouse
``_no_real_wt_fragment`` fixture installs; nothing here can reach a real
Windows Terminal folder."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from magent import icons, wt_profiles
from magent.wt_profiles import IconSpec


@pytest.fixture(autouse=True)
def _icons_on(monkeypatch):
    """These tests are ABOUT the feature: the conftest pin is turned back on in
    process (and the env singleton re-read)."""
    monkeypatch.setenv("MAGENT_WT_ICONS", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


def _png(width: int, height: int) -> bytes:
    return icons._png(width, height, b"\x20\x40\x80\xff" * (width * height))


def _ico() -> bytes:
    # ICONDIR: reserved 0, type 1, one image; the payload is irrelevant here.
    return struct.pack("<HHH", 0, 1, 1) + b"\x00" * 32


def _manifest() -> dict:
    directory = wt_profiles.fragment_dir()
    assert directory is not None
    return json.loads((directory / wt_profiles.FRAGMENT_FILE).read_text("utf-8"))


def _files() -> set[str]:
    directory = wt_profiles.fragment_dir()
    assert directory is not None
    return {p.name for p in directory.iterdir()}


# Bound at import (collection) time, i.e. BEFORE the autouse fixture re-aims the
# module attribute at a tmp dir -- the one way to test the real resolver.
_REAL_FRAGMENTS_ROOT = wt_profiles.fragments_root


class TestTheResolverSeam:
    def test_none_when_localappdata_is_unset(self, monkeypatch):
        monkeypatch.setattr("magent.env.localappdata_dir", Path)
        assert _REAL_FRAGMENTS_ROOT() is None

    def test_real_resolver_builds_the_documented_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr("magent.env.localappdata_dir", lambda: tmp_path)
        assert _REAL_FRAGMENTS_ROOT() == (
            tmp_path / "Microsoft" / "Windows Terminal" / "Fragments"
        )

    def test_the_autouse_guard_redirects_the_module_attribute(self, tmp_path):
        root = wt_profiles.fragments_root()
        assert root is not None
        assert "Microsoft" not in root.parts


class TestProfileName:
    def test_prefixed(self):
        assert wt_profiles.profile_name("alpha") == "magent: alpha"

    @pytest.mark.parametrize("key", ["a;b", 'a"b', "a\nb", "a\x00b"])
    def test_command_separators_and_quotes_never_survive(self, key):
        name = wt_profiles.profile_name(key)
        assert ";" not in name
        assert '"' not in name
        assert not any(ord(c) < 32 for c in name)

    def test_empty_key_still_names_a_profile(self):
        assert wt_profiles.profile_name("") == "magent: project"


class TestDerivedColor:
    def test_stable_and_hex(self):
        a = wt_profiles.derived_color("alpha")
        assert a == wt_profiles.derived_color("alpha")
        assert icons.parse_hex_color(a) is not None

    def test_different_keys_differ(self):
        assert wt_profiles.derived_color("alpha") != wt_profiles.derived_color("beta")


class TestSyncWritesTheFragment:
    def test_one_profile_per_spec_with_a_badge_icon(self, tmp_path):
        result = wt_profiles.sync(
            [
                IconSpec("alpha", "Alpha", "#a855f7"),
                IconSpec("beta", "Beta", "#22c55e"),
            ]
        )
        assert result.error is None
        assert result.changed is True
        doc = _manifest()
        assert [p["name"] for p in doc["profiles"]] == [
            "magent: alpha",
            "magent: beta",
        ]
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        for entry in doc["profiles"]:
            assert entry["hidden"] is wt_profiles.HIDE_PROFILES
            assert entry["commandline"] == "cmd.exe"
            icon = directory / entry["icon"]
            assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            # A bare filename: relative to the fragment folder (WT >= 1.24).
            assert "/" not in entry["icon"]
            assert "\\" not in entry["icon"]

    def test_manifest_is_plain_ascii_json(self):
        wt_profiles.sync([IconSpec("projet-été", "Été", "#112233")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        raw = (directory / wt_profiles.FRAGMENT_FILE).read_bytes()
        raw.decode("ascii")
        assert json.loads(raw)["profiles"][0]["name"] == "magent: projet-été"

    def test_second_identical_sync_changes_nothing(self):
        specs = [IconSpec("alpha", "Alpha", "#a855f7")]
        wt_profiles.sync(specs)
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        manifest = directory / wt_profiles.FRAGMENT_FILE
        before = (manifest.read_bytes(), manifest.stat().st_mtime_ns, _files())
        again = wt_profiles.sync(specs)
        assert again.changed is False
        assert (manifest.read_bytes(), manifest.stat().st_mtime_ns, _files()) == before

    def test_a_changed_colour_is_a_new_icon_path_and_the_old_file_is_pruned(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        old = _manifest()["profiles"][0]["icon"]
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#22c55e")])
        new = _manifest()["profiles"][0]["icon"]
        assert new != old
        assert old not in _files()
        assert new in _files()

    def test_no_colour_falls_back_to_a_derived_one(self):
        wt_profiles.sync([IconSpec("remote-sid", "remote-sid")])
        assert len(_manifest()["profiles"]) == 1

    def test_no_temp_files_are_left_behind(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        assert not [n for n in _files() if n.endswith(".tmp")]

    def test_failure_is_reported_never_raised(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("disk on fire")

        monkeypatch.setattr(wt_profiles, "_atomic_write", boom)
        result = wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        assert result.error == "disk on fire"
        assert wt_profiles.profile_for("alpha") is None


class TestMergeIsAdditive:
    def test_a_second_sync_keeps_the_first_projects_profiles(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        wt_profiles.sync([IconSpec("beta", "Beta", "#22c55e")])
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: alpha", "magent: beta"]
        assert wt_profiles.profile_for("alpha") == "magent: alpha"

    def test_a_resynced_key_moves_to_the_newest_end(self):
        wt_profiles.sync(
            [IconSpec("alpha", "Alpha", "#a855f7"), IconSpec("beta", "B", "#22c55e")]
        )
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: beta", "magent: alpha"]

    def test_the_oldest_profiles_fall_off_at_the_cap(self, monkeypatch):
        monkeypatch.setattr(wt_profiles, "MAX_PROFILES", 3)
        for key in ("a", "b", "c", "d"):
            wt_profiles.sync([IconSpec(key, key, "#336699")])
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: b", "magent: c", "magent: d"]
        # ...and the dropped one's icon file went with it.
        assert len([n for n in _files() if n.endswith(".png")]) == 3


class TestIconSources:
    def test_config_icon_beats_everything(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        (project / "mine.png").write_bytes(_png(24, 24))
        wt_profiles.sync(
            [IconSpec("p", "P", "#112233", project_dir=project, icon="mine.png")]
        )
        entry = _manifest()["profiles"][0]
        assert ".cfg." in entry["icon"]
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        assert (directory / entry["icon"]).read_bytes() == (
            project / "mine.png"
        ).read_bytes()

    def test_absolute_and_home_relative_config_icons(self, tmp_path, monkeypatch):
        icon = tmp_path / "abs.ico"
        icon.write_bytes(_ico())
        wt_profiles.sync([IconSpec("p", "P", None, project_dir=None, icon=str(icon))])
        assert _manifest()["profiles"][0]["icon"].endswith(".ico")

    def test_a_bad_config_icon_falls_through_to_the_next_source(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        (project / "notes.png").write_text("not an image")
        wt_profiles.sync(
            [IconSpec("p", "P", "#112233", project_dir=project, icon="notes.png")]
        )
        assert ".auto." in _manifest()["profiles"][0]["icon"]

    def test_a_logo_the_repo_ships_is_discovered(self, tmp_path):
        project = tmp_path / "proj"
        (project / "public").mkdir(parents=True)
        (project / "public" / "favicon.ico").write_bytes(_ico())
        wt_profiles.sync([IconSpec("p", "P", "#112233", project_dir=project)])
        entry = _manifest()["profiles"][0]
        assert ".auto." in entry["icon"]
        assert entry["icon"].endswith(".ico")

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("favicon.png", b"<svg/>"),  # wrong magic
            ("favicon.ico", b""),  # empty
            ("icon.png", _png(8, 8)),  # too small to read at tab size
            ("logo.png", _png(100, 20)),  # a banner, not a logo
        ],
    )
    def test_unusable_candidates_are_skipped(self, tmp_path, name, data):
        project = tmp_path / "proj"
        project.mkdir()
        (project / name).write_bytes(data)
        wt_profiles.sync([IconSpec("p", "P", "#112233", project_dir=project)])
        assert ".gen." in _manifest()["profiles"][0]["icon"]

    def test_an_oversized_candidate_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_profiles, "MAX_ICON_BYTES", 100)
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(64, 64))
        wt_profiles.sync([IconSpec("p", "P", "#112233", project_dir=project)])
        assert ".gen." in _manifest()["profiles"][0]["icon"]

    def test_a_banner_is_fine_when_explicitly_configured(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "wide.png").write_bytes(_png(100, 20))
        wt_profiles.sync(
            [IconSpec("p", "P", None, project_dir=project, icon="wide.png")]
        )
        assert ".cfg." in _manifest()["profiles"][0]["icon"]

    def test_a_remote_project_without_an_icon_gets_a_badge(self):
        wt_profiles.sync([IconSpec("p", "P", "#112233", project_dir=None)])
        assert ".gen." in _manifest()["profiles"][0]["icon"]

    def test_a_changed_logo_is_a_new_path(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        wt_profiles.sync([IconSpec("p", "P", None, project_dir=project)])
        first = _manifest()["profiles"][0]["icon"]
        (project / "favicon.png").write_bytes(_png(48, 48))
        wt_profiles.sync([IconSpec("p", "P", None, project_dir=project)])
        assert _manifest()["profiles"][0]["icon"] != first


class TestProfileFor:
    def test_none_before_any_sync(self):
        assert wt_profiles.profile_for("alpha") is None

    def test_name_once_synced(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        assert wt_profiles.profile_for("alpha") == "magent: alpha"
        assert wt_profiles.profile_for("never-synced") is None

    def test_none_when_the_icon_file_is_gone(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        for png in directory.glob("*.png"):
            png.unlink()
        assert wt_profiles.profile_for("alpha") is None

    def test_a_corrupt_manifest_reads_as_empty(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        (directory / wt_profiles.FRAGMENT_FILE).write_text("{not json", "utf-8")
        assert wt_profiles.profile_for("alpha") is None
        assert wt_profiles.read_fragment() == []

    def test_none_without_a_resolvable_fragment_root(self, monkeypatch):
        monkeypatch.setattr(wt_profiles, "fragments_root", lambda: None)
        assert wt_profiles.profile_for("alpha") is None


class TestKillSwitches:
    def test_env_off_writes_nothing_and_answers_none(self, monkeypatch):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        monkeypatch.setenv("MAGENT_WT_ICONS", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert wt_profiles.profile_for("alpha") is None
        before = _files()
        result = wt_profiles.sync([IconSpec("beta", "Beta", "#22c55e")])
        assert result.skipped == "MAGENT_WT_ICONS=0"
        assert _files() == before

    def test_setting_off_removes_the_fragment(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        result = wt_profiles.sync(
            [IconSpec("alpha", "Alpha", "#a855f7")], setting=False
        )
        assert result.skipped == "disabled"
        assert result.changed is True
        assert wt_profiles.fragment_dir() is not None
        assert not wt_profiles.fragment_dir().exists()
        assert wt_profiles.profile_for("alpha") is None

    def test_no_localappdata_is_a_clean_skip(self, monkeypatch):
        monkeypatch.setattr(wt_profiles, "fragments_root", lambda: None)
        result = wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        assert result.skipped == "no LOCALAPPDATA"
        assert result.error is None

    def test_a_garbage_env_value_degrades_to_enabled(self, monkeypatch):
        monkeypatch.setenv("MAGENT_WT_ICONS", "maybe")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert wt_profiles.enabled() is True


class TestRemove:
    def test_removes_only_magents_folder(self, tmp_path):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        root = wt_profiles.fragments_root()
        assert root is not None
        sibling = root / "someone-else"
        sibling.mkdir()
        (sibling / "theirs.json").write_text("{}", "utf-8")
        assert wt_profiles.remove() is True
        assert not (root / wt_profiles.APP_NAME).exists()
        assert (sibling / "theirs.json").exists()

    def test_nothing_to_remove_is_false(self):
        assert wt_profiles.remove() is False


class TestPruneNeverTouchesWhatItDidNotWrite:
    def test_a_foreign_json_beside_the_manifest_survives(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        (directory / "other.json").write_text("{}", "utf-8")
        wt_profiles.sync([IconSpec("beta", "Beta", "#22c55e")])
        assert "other.json" in _files()
