"""projects + config_io: validated, backed-up, atomic config writes."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from magent import config_io, projects, psmux
from magent.lockfile import LockHeld, persistent_lock


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "magent.config.json"
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "projects": [
                    {"path": "work/api", "group": "WORK"},
                    {"path": "work/web", "title": "Site", "enabled": False},
                ],
                "settings": {"someFutureKey": {"kept": True}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class TestConfigIo:
    def test_save_round_trips_unknown_keys(self, cfg):
        data = config_io.load_raw(cfg)
        config_io.save(cfg, data)
        assert config_io.load_raw(cfg)["settings"] == {"someFutureKey": {"kept": True}}

    def test_invalid_content_leaves_the_file_byte_identical(self, cfg):
        before = cfg.read_bytes()
        with pytest.raises(config_io.ConfigWriteError):
            config_io.save(cfg, {"projects": "not a list"})
        assert cfg.read_bytes() == before
        assert not list(config_io.backups_dir().glob("config-*.json"))

    def test_a_crash_before_the_swap_leaves_the_old_file(self, cfg, monkeypatch):
        before = cfg.read_bytes()

        def _crash(_src, _dst):
            raise OSError("power cut")

        monkeypatch.setattr(os, "replace", _crash)
        with pytest.raises(OSError, match="power cut"):
            config_io.write_atomic(cfg, {"version": 4, "projects": []})
        assert cfg.read_bytes() == before
        assert not list(cfg.parent.glob("*.tmp"))

    def test_every_save_backs_up_the_previous_file(self, cfg):
        before = cfg.read_bytes()
        config_io.save(cfg, config_io.load_raw(cfg))
        [backup] = config_io.backups_dir().glob("config-*.json")
        assert backup.read_bytes() == before

    def test_backups_keep_only_the_newest(self, cfg):
        for _ in range(5):
            config_io.backup(cfg, keep=3)
        assert len(list(config_io.backups_dir().glob("config-*.json"))) == 3

    def test_backups_sort_in_the_order_they_were_made(self, cfg):
        made = [config_io.backup(cfg, keep=10) for _ in range(5)]
        assert made == sorted(made)
        assert sorted(config_io.backups_dir().glob("config-*.json")) == made

    def test_each_config_file_keeps_its_own_backups(self, cfg, tmp_path):
        other = tmp_path / "other.json"
        other.write_bytes(cfg.read_bytes())
        for _ in range(4):
            config_io.backup(cfg, keep=2)
        config_io.backup(other, keep=2)
        folder = config_io.backups_dir()
        assert len(list(folder.glob(config_io.backup_prefix(cfg) + "*.json"))) == 2
        assert len(list(folder.glob(config_io.backup_prefix(other) + "*.json"))) == 1
        assert config_io.backup_prefix(cfg) != config_io.backup_prefix(other)

    def test_a_reader_holding_the_file_is_waited_out(self, cfg, monkeypatch):
        # Windows: os.replace onto a file another process is reading raises
        # PermissionError for as long as the read holds it.
        real_replace = os.replace
        refusals = [PermissionError(13, "held by a reader")] * 2
        sleeps: list[float] = []

        def flaky(src, dst):
            if refusals:
                raise refusals.pop()
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky)
        monkeypatch.setattr(time, "sleep", sleeps.append)
        config_io.save(cfg, {"version": 4, "projects": []})
        assert config_io.load_raw(cfg) == {"version": 4, "projects": []}
        assert sleeps == [config_io.REPLACE_SLEEP_S] * 2
        assert not list(cfg.parent.glob("*.tmp"))

    def test_a_file_held_past_every_retry_raises_and_keeps_the_old_one(
        self, cfg, monkeypatch
    ):
        before = cfg.read_bytes()
        attempts: list[object] = []

        def refuse(src, dst):
            attempts.append(src)
            raise PermissionError(13, "held by a reader")

        monkeypatch.setattr(os, "replace", refuse)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            config_io.write_atomic(cfg, {"version": 4, "projects": []})
        assert len(attempts) == config_io.REPLACE_RETRIES + 1
        assert cfg.read_bytes() == before

    def test_load_raw_refuses_a_non_object(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text("[]", encoding="utf-8")
        with pytest.raises(TypeError):
            config_io.load_raw(bad)


class TestLocked:
    def test_writers_in_one_process_queue_instead_of_refusing(self, cfg):
        # Two API requests on two serve threads: both land, neither is a 409.
        errors: list[BaseException] = []
        started = threading.Barrier(2)

        def add(path):
            try:
                started.wait(timeout=5)
                projects.add(str(cfg), path)
            except BaseException as exc:  # noqa: BLE001  # reason: any failure in a thread must reach the assertion
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(p,)) for p in ("a/one", "b/two")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert errors == []
        names = [p.name for p in projects.list_projects(str(cfg))]
        assert names == ["api", "Site", "one", "two"] or names == [
            "api",
            "Site",
            "two",
            "one",
        ]

    def test_another_process_s_writer_is_waited_for(self, cfg):
        # The file lock taken on its own, as another magent process holds it,
        # released shortly after: the write waits for it rather than refusing.
        acquired = threading.Event()
        released = threading.Event()

        def hold():
            with persistent_lock(config_io.LOCK_NAME, wait_s=0.0):
                acquired.set()
                released.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        assert acquired.wait(timeout=5)
        timer = threading.Timer(0.1, released.set)
        timer.start()
        try:
            assert projects.add(str(cfg), "work/cli").name == "cli"
        finally:
            released.set()
            holder.join(timeout=5)

    def test_a_lock_that_stays_held_is_a_conflict(self, cfg, monkeypatch):
        monkeypatch.setattr(config_io, "LOCK_WAIT_S", 0.05)
        before = cfg.read_bytes()
        with (
            persistent_lock(config_io.LOCK_NAME, wait_s=0.0),
            pytest.raises(projects.ProjectError) as err,
        ):
            projects.add(str(cfg), "work/cli")
        assert err.value.code == "conflict"
        assert cfg.read_bytes() == before

    def test_locked_raises_lock_held_itself(self, monkeypatch):
        with (
            persistent_lock(config_io.LOCK_NAME, wait_s=0.0),
            pytest.raises(LockHeld),
            config_io.locked(wait_s=0.05),
        ):
            pass


class TestList:
    def test_lists_every_project_with_its_shape(self, cfg):
        api, site = projects.list_projects(str(cfg))
        assert api == projects.Project(
            name="api",
            session="api",
            path="work/api",
            group="WORK",
            tool=None,
            enabled=True,
            node=None,
            windows=1,
        )
        assert (site.name, site.session, site.enabled) == ("Site", "Site", False)

    def test_a_missing_config_is_not_found(self, tmp_path):
        with pytest.raises(projects.ProjectError) as err:
            projects.list_projects(str(tmp_path / "absent.json"))
        assert err.value.code == "not_found"


class TestAdd:
    def test_appends_and_returns_the_project(self, cfg):
        added = projects.add(str(cfg), "work/cli", group="WORK", tool="codex")
        assert (added.name, added.group, added.tool) == ("cli", "WORK", "codex")
        assert config_io.load_raw(cfg)["projects"][-1] == {
            "path": "work/cli",
            "group": "WORK",
            "tool": "codex",
        }

    def test_a_duplicate_session_is_a_conflict_and_nothing_changes(self, cfg):
        before = cfg.read_bytes()
        with pytest.raises(projects.ProjectError) as err:
            projects.add(str(cfg), "elsewhere/api")
        assert (err.value.code, err.value.details) == (
            "conflict",
            {"reason": "duplicate_session"},
        )
        assert cfg.read_bytes() == before

    def test_a_config_that_would_not_load_is_refused(self, cfg):
        before = cfg.read_bytes()
        with pytest.raises(projects.ProjectError) as err:
            projects.add(str(cfg), "work/cli", node="nosuchnode")
        assert (err.value.code, err.value.details) == (
            "invalid_request",
            {"reason": "invalid_config"},
        )
        assert cfg.read_bytes() == before

    def test_an_unwritable_backups_dir_is_unavailable_and_nothing_changes(
        self, cfg, tmp_path, monkeypatch
    ):
        blocker = tmp_path / "backups-is-a-file"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(config_io, "backups_dir", lambda: blocker)
        before = cfg.read_bytes()
        with pytest.raises(projects.ProjectError) as err:
            projects.add(str(cfg), "work/cli")
        assert (err.value.code, err.value.details) == (
            "unavailable",
            {"reason": "write_failed"},
        )
        assert cfg.read_bytes() == before

    def test_an_empty_path_is_invalid(self, cfg):
        with pytest.raises(projects.ProjectError) as err:
            projects.add(str(cfg), "  ")
        assert err.value.code == "invalid_request"


class TestRemove:
    def test_removes_by_leaf_name(self, cfg):
        result = projects.remove(str(cfg), "api")
        assert result == projects.RemoveResult(removed=["api"], stopped=[])
        assert [p.name for p in projects.list_projects(str(cfg))] == ["Site"]

    def test_removes_by_title(self, cfg):
        assert projects.remove(str(cfg), "Site").removed == ["Site"]

    def test_no_match_is_not_found_and_nothing_changes(self, cfg):
        before = cfg.read_bytes()
        with pytest.raises(projects.ProjectError) as err:
            projects.remove(str(cfg), "ghost")
        assert err.value.code == "not_found"
        assert cfg.read_bytes() == before

    def test_stop_stops_the_session_first(self, cfg, monkeypatch):
        seen = []

        def _stop(names, psmux=None):
            seen.append(list(names))
            return list(names), []

        monkeypatch.setattr(psmux, "stop_sessions", _stop)
        result = projects.remove(str(cfg), "api", stop=True)
        assert seen == [["api"]]
        assert result.stopped == ["api"]

    def test_a_stop_that_fails_is_unavailable_and_nothing_is_removed(
        self, cfg, monkeypatch
    ):
        def _stop(names, psmux=None):
            raise OSError("psmux: socket gone")

        monkeypatch.setattr(psmux, "stop_sessions", _stop)
        before = cfg.read_bytes()
        with pytest.raises(projects.ProjectError) as err:
            projects.remove(str(cfg), "api", stop=True)
        assert (err.value.code, err.value.details) == (
            "unavailable",
            {"reason": "stop_failed"},
        )
        assert cfg.read_bytes() == before


class TestSetEnabled:
    def test_disables_and_enables(self, cfg):
        assert projects.set_enabled(str(cfg), "api", False).enabled is False
        assert config_io.load_raw(cfg)["projects"][0]["enabled"] is False
        assert projects.set_enabled(str(cfg), "Site", True).enabled is True

    def test_no_match_is_not_found(self, cfg):
        with pytest.raises(projects.ProjectError) as err:
            projects.set_enabled(str(cfg), "ghost", True)
        assert err.value.code == "not_found"
