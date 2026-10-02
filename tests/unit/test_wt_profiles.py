"""wt_profiles: the magent-owned Windows Terminal fragment that gives every tab
a real image icon. Everything runs against the tmp fragments root the autouse
``_no_real_wt_fragment`` fixture installs; nothing here can reach a real
Windows Terminal folder."""

from __future__ import annotations

import json
import os
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


def _ico(payload: int = 32) -> bytes:
    # ICONDIR (reserved 0, type 1, one image) + ONE directory entry whose
    # (size, offset) really lie inside the file; the payload bytes are opaque.
    entry = struct.pack("<BBBBHHII", 16, 16, 0, 0, 1, 32, payload, 6 + 16)
    return struct.pack("<HHH", 0, 1, 1) + entry + b"\x00" * payload


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
        assert wt_profiles.profile_name("").startswith("magent: project-")

    def test_an_untouched_key_keeps_its_plain_name(self):
        assert wt_profiles.profile_name("my_proj-2") == "magent: my_proj-2"

    def test_keys_that_sanitize_alike_stay_three_profiles(self):
        names = {
            wt_profiles.profile_name(k) for k in ("a;b", 'a"b', "a_b", "a\nb", " a_b")
        }
        assert len(names) == 5
        # ...and the real `a_b` is the one that keeps the plain name.
        assert wt_profiles.profile_name("a_b") == "magent: a_b"

    def test_the_name_is_stable(self):
        assert wt_profiles.profile_name("a;b") == wt_profiles.profile_name("a;b")


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

    def test_an_unchanged_resync_keeps_its_place(self):
        wt_profiles.sync(
            [IconSpec("alpha", "Alpha", "#a855f7"), IconSpec("beta", "B", "#22c55e")]
        )
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: alpha", "magent: beta"]

    def test_a_changed_profile_moves_to_the_newest_end(self):
        wt_profiles.sync(
            [IconSpec("alpha", "Alpha", "#a855f7"), IconSpec("beta", "B", "#22c55e")]
        )
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#112233")])
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: beta", "magent: alpha"]

    def test_repeated_single_spec_syncs_leave_the_manifest_untouched(self):
        # The per-window pattern (and the one a phase that cannot batch still
        # uses): with other profiles beside it, re-syncing one window must not
        # rewrite the file -- not its bytes, not its mtime, not its order.
        specs = [
            IconSpec("alpha", "Alpha", "#a855f7"),
            IconSpec("beta", "Beta", "#22c55e"),
            IconSpec("gamma", "Gamma", "#336699"),
        ]
        wt_profiles.sync(specs)
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        manifest = directory / wt_profiles.FRAGMENT_FILE
        before = (manifest.read_bytes(), manifest.stat().st_mtime_ns)
        for spec in (specs[0], specs[1], specs[0], specs[2], specs[1]):
            assert wt_profiles.sync([spec]).changed is False
        assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before

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


class TestIconValidation:
    def _png_file(self, tmp_path, data, name="favicon.png"):
        project = tmp_path / "proj"
        project.mkdir(exist_ok=True)
        (project / name).write_bytes(data)
        return project

    def _source(self, project, icon=None):
        wt_profiles.sync(
            [IconSpec("p", "P", "#112233", project_dir=project, icon=icon)]
        )
        return _manifest()["profiles"][0]["icon"]

    def test_a_png_without_iend_is_refused(self, tmp_path):
        whole = _png(32, 32)
        assert wt_profiles._classify(whole, require_square=True) == "png"
        cut = whole[: len(whole) - 12]  # the IEND chunk (12 bytes) is gone
        assert wt_profiles._classify(cut, require_square=True) is None

    def test_a_png_truncated_mid_stream_falls_through_to_the_badge(self, tmp_path):
        whole = _png(32, 32)
        project = self._png_file(tmp_path, whole[: len(whole) // 2])
        assert ".gen." in self._source(project)

    def test_a_png_with_a_malformed_ihdr_is_refused(self):
        whole = bytearray(_png(32, 32))
        whole[8:12] = b"\x00\x00\x00\x0a"  # an IHDR length of 10, not 13
        assert wt_profiles._classify(bytes(whole), require_square=True) is None

    def test_an_ico_whose_image_runs_past_the_file_is_refused(self):
        good = _ico(32)
        assert wt_profiles._classify(good, require_square=True) == "ico"
        assert wt_profiles._classify(good[:-8], require_square=True) is None

    def test_an_ico_with_a_zero_count_or_a_table_past_the_end_is_refused(self):
        zero = struct.pack("<HHH", 0, 1, 0) + b"\x00" * 32
        huge = struct.pack("<HHH", 0, 1, 40000) + b"\x00" * 32
        assert wt_profiles._classify(zero, require_square=True) is None
        assert wt_profiles._classify(huge, require_square=True) is None

    def test_an_ico_entry_pointing_into_the_directory_is_refused(self):
        entry = struct.pack("<BBBBHHII", 16, 16, 0, 0, 1, 32, 8, 2)  # offset 2
        junk = struct.pack("<HHH", 0, 1, 1) + entry + b"\x00" * 16
        assert wt_profiles._classify(junk, require_square=True) is None

    def test_a_junk_ico_falls_through_to_the_next_candidate(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.ico").write_bytes(b"\x00\x00\x01\x00\x01\x00junk")
        (project / "favicon.png").write_bytes(_png(32, 32))
        assert ".auto." in self._source(project)
        assert _manifest()["profiles"][0]["icon"].endswith(".png")

    def test_a_nul_in_the_icon_path_is_a_clean_miss_not_a_crash(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        icon = self._source(project, icon="bad\x00name.png")
        assert ".gen." in icon

    def test_a_relative_icon_with_no_project_directory_is_logged(self, caplog):
        with caplog.at_level("WARNING"):
            wt_profiles.sync([IconSpec("p", "P", "#112233", icon="logo.png")])
        assert any("no project directory" in r.getMessage() for r in caplog.records)
        assert ".gen." in _manifest()["profiles"][0]["icon"]


class TestOneBadSpecDoesNotSpoilTheBatch:
    def test_a_spec_that_raises_costs_only_its_own_profile(self, monkeypatch):
        real = wt_profiles._resolve

        def flaky(spec, cache):
            if spec.key == "bad":
                raise OSError("denied")
            return real(spec, cache)

        monkeypatch.setattr(wt_profiles, "_resolve", flaky)
        result = wt_profiles.sync(
            [
                IconSpec("good1", "G", "#112233"),
                IconSpec("bad", "B", "#112233"),
                IconSpec("good2", "H", "#223344"),
            ]
        )
        assert result.error is None
        names = [p["name"] for p in _manifest()["profiles"]]
        assert names == ["magent: good1", "magent: good2"]
        # The first spec's icon file was written and is still referenced, not
        # orphaned and pruned.
        assert len([n for n in _files() if n.endswith(".png")]) == 2
        assert wt_profiles.profile_for("good1") == "magent: good1"

    def test_a_source_that_breaks_at_read_time_falls_back_to_the_badge(
        self, tmp_path, monkeypatch
    ):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        real = Path.read_bytes
        broken = {"on": False}

        def hostile(self):
            if broken["on"] and self.name == "favicon.png":
                raise OSError("cloud file will not hydrate")
            return real(self)

        # First launch caches the logo; the icon file is then lost, so the next
        # one has to read the (now unreadable) logo again to write it.
        spec = IconSpec("p", "P", "#112233", project_dir=project)
        wt_profiles.sync([spec])
        for png in wt_profiles.fragment_dir().glob("*.png"):
            png.unlink()
        monkeypatch.setattr(Path, "read_bytes", hostile)
        broken["on"] = True
        wt_profiles.sync([spec])
        broken["on"] = False
        assert ".gen." in _manifest()["profiles"][0]["icon"]
        assert wt_profiles.profile_for("p") == "magent: p"


class TestTheLockCostsOneWaitPerLaunch:
    def test_the_wait_is_short(self):
        assert wt_profiles.LOCK_WAIT_S <= 1.5

    def _hold(self, monkeypatch):
        waits: list[float] = []

        def held(_name, *, wait_s):
            waits.append(wait_s)
            raise wt_profiles.LockHeld("held")

        monkeypatch.setattr(wt_profiles, "persistent_lock", held)
        return waits

    def test_after_one_held_lock_the_rest_of_the_launch_does_not_wait(
        self, monkeypatch
    ):
        waits = self._hold(monkeypatch)
        first = wt_profiles.sync([IconSpec("a", "A", "#112233")])
        assert first.error is not None
        for key in "bcd":
            again = wt_profiles.sync([IconSpec(key, key, "#112233")])
            assert again.skipped is not None
            assert again.error is None
        assert len(waits) == 1  # ONE bounded wait for the whole launch

    def test_begin_launch_lets_the_next_launch_try_again(self, monkeypatch):
        waits = self._hold(monkeypatch)
        wt_profiles.sync([IconSpec("a", "A", "#112233")])
        wt_profiles.begin_launch()
        wt_profiles.sync([IconSpec("a", "A", "#112233")])
        assert len(waits) == 2

    def test_a_skipped_sync_writes_nothing(self, monkeypatch):
        self._hold(monkeypatch)
        wt_profiles.sync([IconSpec("a", "A", "#112233")])
        wt_profiles.sync([IconSpec("b", "B", "#112233")])
        directory = wt_profiles.fragment_dir()
        assert directory is None or not directory.exists()


class TestAnAttachGuessNeverFlapsALaunchProfile:
    def test_only_if_missing_leaves_an_existing_profile_byte_for_byte(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        manifest = directory / wt_profiles.FRAGMENT_FILE
        before = (manifest.read_bytes(), manifest.stat().st_mtime_ns, _files())
        # An attach to a host whose session is ALSO called `alpha`: a guess in a
        # derived colour must not take the tab's icon away from the launch's.
        result = wt_profiles.sync(
            [IconSpec("alpha", "alpha", None, only_if_missing=True)]
        )
        assert result.changed is False
        assert (manifest.read_bytes(), manifest.stat().st_mtime_ns, _files()) == before

    def test_the_two_writers_taking_turns_do_not_change_anything(self):
        launch = IconSpec("alpha", "Alpha", "#a855f7")
        attach = IconSpec("alpha", "alpha", None, only_if_missing=True)
        wt_profiles.sync([launch])
        icon = _manifest()["profiles"][0]["icon"]
        for _ in range(3):
            wt_profiles.sync([attach])
            wt_profiles.sync([launch])
            assert _manifest()["profiles"][0]["icon"] == icon

    def test_a_missing_profile_is_still_created(self):
        wt_profiles.sync(
            [IconSpec("remote-sid", "remote-sid", None, only_if_missing=True)]
        )
        assert wt_profiles.profile_for("remote-sid") == "magent: remote-sid"

    def test_a_profile_whose_icon_file_is_gone_is_rewritten(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        for png in wt_profiles.fragment_dir().glob("*.png"):
            png.unlink()
        wt_profiles.sync([IconSpec("alpha", "alpha", None, only_if_missing=True)])
        assert wt_profiles.profile_for("alpha") == "magent: alpha"


class TestRemoveOnlyDeletesWhatMagentOwns:
    def _synced(self):
        wt_profiles.sync([IconSpec("alpha", "Alpha", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        return directory

    def test_a_foreign_file_stays_and_so_does_the_folder(self):
        directory = self._synced()
        (directory / "notes.txt").write_text("mine", "utf-8")
        (directory / "theirs.json").write_text("{}", "utf-8")
        assert wt_profiles.remove() is False
        assert (directory / "notes.txt").exists()
        assert (directory / "theirs.json").exists()
        # ...but everything magent wrote is gone.
        assert not (directory / wt_profiles.FRAGMENT_FILE).exists()
        assert not list(directory.glob("*.png"))

    def test_the_sidecar_and_stray_temp_files_go_with_the_folder(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        wt_profiles.sync([IconSpec("p", "P", None, project_dir=project)])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        assert (directory / wt_profiles.ICON_CACHE_FILE).exists()
        (directory / ".magent.json.1234.tmp").write_bytes(b"x")
        assert wt_profiles.remove() is True
        assert not directory.exists()

    def test_it_takes_the_lock(self, monkeypatch):
        directory = self._synced()
        taken: list[str] = []
        real = wt_profiles.persistent_lock

        def spy(name, *, wait_s):
            taken.append(name)
            return real(name, wait_s=wait_s)

        monkeypatch.setattr(wt_profiles, "persistent_lock", spy)
        assert wt_profiles.remove() is True
        assert taken == [wt_profiles.LOCK_NAME]
        assert not directory.exists()

    def test_a_held_lock_removes_nothing(self, monkeypatch):
        directory = self._synced()

        def held(_name, *, wait_s):
            raise wt_profiles.LockHeld("held")

        monkeypatch.setattr(wt_profiles, "persistent_lock", held)
        assert wt_profiles.remove() is False
        assert (directory / wt_profiles.FRAGMENT_FILE).exists()


class TestDiscoveryIsCachedByStat:
    def _reads(self, monkeypatch):
        calls: list[str] = []
        real = wt_profiles._read_icon_file

        def counting(path, *, require_square):
            calls.append(path.name)
            return real(path, require_square=require_square)

        monkeypatch.setattr(wt_profiles, "_read_icon_file", counting)
        return calls

    def test_an_unchanged_logo_is_read_once_across_launches(
        self, tmp_path, monkeypatch
    ):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        calls = self._reads(monkeypatch)
        spec = IconSpec("p", "P", None, project_dir=project)
        wt_profiles.sync([spec])
        assert calls == ["favicon.png"]
        for _ in range(3):
            wt_profiles.begin_launch()
            assert wt_profiles.sync([spec]).changed is False
        assert calls == ["favicon.png"]

    def test_the_icon_file_is_still_written_from_a_cache_hit(
        self, tmp_path, monkeypatch
    ):
        project = tmp_path / "proj"
        project.mkdir()
        logo = _png(32, 32)
        (project / "favicon.png").write_bytes(logo)
        spec = IconSpec("p", "P", None, project_dir=project)
        wt_profiles.sync([spec])
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        for png in directory.glob("*.png"):
            png.unlink()
        wt_profiles.sync([spec])  # cache hit; bytes are re-read only to write
        entry = _manifest()["profiles"][0]
        assert (directory / entry["icon"]).read_bytes() == logo

    def test_an_invalid_candidate_is_remembered_too(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.ico").write_bytes(b"<svg/>")
        calls = self._reads(monkeypatch)
        spec = IconSpec("p", "P", "#112233", project_dir=project)
        wt_profiles.sync([spec])
        wt_profiles.sync([spec])
        assert calls == ["favicon.ico"]

    def test_a_changed_file_is_read_again(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "favicon.png").write_bytes(_png(32, 32))
        calls = self._reads(monkeypatch)
        spec = IconSpec("p", "P", None, project_dir=project)
        wt_profiles.sync([spec])
        (project / "favicon.png").write_bytes(_png(48, 48))
        wt_profiles.sync([spec])
        assert calls == ["favicon.png", "favicon.png"]
        assert wt_profiles.profile_for("p") == "magent: p"

    def test_a_file_swapped_behind_a_stale_cache_is_refused_not_trusted(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        logo = project / "favicon.png"
        logo.write_bytes(_png(32, 32))
        spec = IconSpec("p", "P", None, project_dir=project)
        wt_profiles.sync([spec])
        stat = logo.stat()
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        for png in directory.glob("*.png"):
            png.unlink()
        # Same size, same mtime, DIFFERENT bytes: the cache vouches for the old
        # stamp, the content check must catch it and draw the badge instead.
        other = bytearray(logo.read_bytes())
        other[40] ^= 0xFF
        logo.write_bytes(bytes(other))
        os.utime(logo, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        wt_profiles.sync([spec])
        assert ".gen." in _manifest()["profiles"][0]["icon"]

    def test_the_sidecar_is_not_a_json_file_windows_terminal_would_read(self):
        assert not wt_profiles.ICON_CACHE_FILE.endswith(".json")

    def test_the_cache_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_profiles, "MAX_CACHE_ENTRIES", 5)
        project = tmp_path / "proj"
        project.mkdir()
        for i in range(8):
            (project / f"x{i}.png").write_bytes(_png(32 + i, 32 + i))
            wt_profiles.sync(
                [IconSpec(f"k{i}", "K", None, project_dir=project, icon=f"x{i}.png")]
            )
        directory = wt_profiles.fragment_dir()
        assert directory is not None
        rows = json.loads((directory / wt_profiles.ICON_CACHE_FILE).read_text("utf-8"))
        assert len(rows["rows"]) <= 5
