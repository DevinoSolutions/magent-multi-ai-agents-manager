import json
import os
import sys
import time

import pytest

from magent.discover import (
    _find_base_dir,
    _is_real_project,
    _uri_to_path,
    discover_projects,
    projects_to_config,
)


class TestIsRealProject:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_rejects_shallow_windows_path(self):
        assert not _is_real_project(r"C:\Users\amind")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_rejects_generic_dir_windows(self):
        assert not _is_real_project(r"C:\Users\amind\OneDrive\Desktop\Projects")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
    def test_accepts_deep_windows_path(self):
        assert _is_real_project(r"C:\Users\amind\OneDrive\Desktop\Projects\myapp")

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix paths")
    def test_rejects_shallow_unix_path(self):
        assert not _is_real_project("/home/user")

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix paths")
    def test_accepts_deep_unix_path(self):
        assert _is_real_project("/home/user/projects/myapp")

    def test_rejects_generic_leaf_name(self):
        if sys.platform == "win32":
            assert not _is_real_project(r"C:\Users\amind\stuff\deep\Desktop")
        else:
            assert not _is_real_project("/home/user/stuff/Desktop")


class TestUriToPath:
    def test_file_uri(self):
        result = _uri_to_path("file:///home/user/project")
        if sys.platform == "win32":
            assert result == "home/user/project"
        else:
            assert result == "/home/user/project"

    def test_percent_encoded_uri(self):
        result = _uri_to_path("file:///home/user/my%20project")
        assert "my project" in result

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows URI")
    def test_windows_file_uri(self):
        result = _uri_to_path("file:///c%3A/Users/amind/project")
        assert result == "c:/Users/amind/project"

    def test_non_file_uri_returns_none(self):
        assert _uri_to_path("https://example.com") is None

    def test_empty_uri_returns_none(self):
        assert _uri_to_path("") is None


class TestFindBaseDir:
    def test_finds_deepest_common(self, tmp_path):
        shared = tmp_path / "projects"
        for name in ("a", "b", "c", "d", "e"):
            (shared / name).mkdir(parents=True)
        paths = [str(shared / n) for n in ("a", "b", "c", "d", "e")]
        result = _find_base_dir(paths)
        assert os.path.normpath(result) == os.path.normpath(str(shared))

    def test_majority_wins_over_outlier(self, tmp_path):
        shared = tmp_path / "projects" / "deep"
        shared.mkdir(parents=True)
        outlier = tmp_path / "other" / "thing"
        outlier.mkdir(parents=True)
        for name in ("a", "b", "c", "d"):
            (shared / name).mkdir()
        (outlier / "x").mkdir()

        paths = [str(shared / n) for n in ("a", "b", "c", "d")] + [str(outlier / "x")]
        result = _find_base_dir(paths)
        assert os.path.normpath(result) == os.path.normpath(str(shared))

    def test_single_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        result = _find_base_dir([str(proj)])
        assert os.path.normpath(result) == os.path.normpath(str(proj))

    def test_empty_list(self):
        assert _find_base_dir([]) == ""


class TestProjectsToConfig:
    def test_groups_from_parent_dir(self, tmp_path):
        base = tmp_path / "projects"
        for group in ("INTERNAL", "LEAD-GEN"):
            for name in ("app1", "app2"):
                (base / group / name).mkdir(parents=True)

        projects = [
            {
                "path": str(base / "INTERNAL" / "app1"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
            {
                "path": str(base / "INTERNAL" / "app2"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
            {
                "path": str(base / "LEAD-GEN" / "app1"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
            {
                "path": str(base / "LEAD-GEN" / "app2"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
        ]
        config = projects_to_config(projects)
        groups = {p.get("group") for p in config["projects"]}
        assert "INTERNAL" in groups
        assert "LEAD-GEN" in groups

    def test_vscode_tool_preserved(self, tmp_path):
        proj = tmp_path / "group" / "app"
        proj.mkdir(parents=True)
        projects = [
            {"path": str(proj), "tool": "vscode", "session_count": 1, "last_active": 1},
        ]
        config = projects_to_config(projects)
        assert config["projects"][0]["tool"] == "vscode"

    def test_claude_tool_omitted_as_default(self, tmp_path):
        proj = tmp_path / "group" / "app"
        proj.mkdir(parents=True)
        projects = [
            {"path": str(proj), "tool": "claude", "session_count": 1, "last_active": 1},
        ]
        config = projects_to_config(projects)
        assert "tool" not in config["projects"][0]

    def test_duplicate_leaf_gets_title(self, tmp_path):
        base = tmp_path / "projects"
        (base / "a" / "app").mkdir(parents=True)
        (base / "b" / "app").mkdir(parents=True)
        projects = [
            {
                "path": str(base / "a" / "app"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
            {
                "path": str(base / "b" / "app"),
                "tool": "claude",
                "session_count": 1,
                "last_active": 1,
            },
        ]
        config = projects_to_config(projects)
        titles = [p.get("title") for p in config["projects"]]
        assert all(t is not None for t in titles)


class TestDiscoverProjects:
    @pytest.fixture(autouse=True)
    def _isolate_vscode_global(self, monkeypatch):
        """Isolate the non-hermetic global `_discover_vscode_projects`, which
        ignores `home` and reads live OS storage -- keeps these codex/claude-
        focused tests deterministic. The real three-way merge (including the
        vscode source) is covered in test_discover_merge.py."""
        import magent.discover

        monkeypatch.setattr(magent.discover, "_discover_vscode_projects", list)

    def test_returns_tuple(self, tmp_path):
        result = discover_projects(home=tmp_path)
        assert isinstance(result, tuple)
        assert len(result) == 2
        projects, days = result
        assert isinstance(projects, list)
        assert isinstance(days, int)

    def test_empty_home_returns_empty(self, tmp_path):
        projects, days = discover_projects(home=tmp_path)
        assert projects == []
        assert days == 0

    def test_finds_codex_sessions(self, tmp_path):
        proj_dir = tmp_path / "deep" / "nested" / "projects" / "myapp"
        proj_dir.mkdir(parents=True)

        sess_dir = tmp_path / ".codex" / "sessions" / "2026" / "01"
        sess_dir.mkdir(parents=True)
        sess_file = sess_dir / "sess.jsonl"
        sess_file.write_text(
            json.dumps(
                {
                    "payload": {"id": "abc", "cwd": str(proj_dir)},
                }
            )
            + "\n"
        )

        projects, _days = discover_projects(home=tmp_path)
        codex_projects = [p for p in projects if p["tool"] == "codex"]
        assert len(codex_projects) == 1
        assert codex_projects[0]["path"] == os.path.normpath(str(proj_dir))

    def test_progressive_window_expands(self, tmp_path):
        proj_dir = tmp_path / "deep" / "nested" / "projects" / "old_app"
        proj_dir.mkdir(parents=True)

        sess_dir = tmp_path / ".codex" / "sessions" / "2025" / "01"
        sess_dir.mkdir(parents=True)
        sess_file = sess_dir / "old.jsonl"
        sess_file.write_text(
            json.dumps(
                {
                    "payload": {"id": "old", "cwd": str(proj_dir)},
                }
            )
            + "\n"
        )
        old_time = time.time() - (90 * 86400)
        os.utime(sess_file, (old_time, old_time))

        projects, days = discover_projects(home=tmp_path)
        assert projects
        assert days >= 90

    def test_claude_preferred_over_codex_when_newer(self, tmp_path):
        proj_dir = tmp_path / "deep" / "nested" / "projects" / "myapp"
        proj_dir.mkdir(parents=True)

        from magent.sessions.claude import encode_claude_project_path

        encoded = encode_claude_project_path(str(proj_dir))
        claude_dir = tmp_path / ".claude" / "projects" / encoded
        claude_dir.mkdir(parents=True)
        claude_sess = claude_dir / "newer.jsonl"
        claude_sess.write_text('{"test": true}\n')

        codex_dir = tmp_path / ".codex" / "sessions" / "2025"
        codex_dir.mkdir(parents=True)
        codex_sess = codex_dir / "older.jsonl"
        codex_sess.write_text(
            json.dumps(
                {
                    "payload": {"id": "old", "cwd": str(proj_dir)},
                }
            )
            + "\n"
        )
        old_time = time.time() - 86400
        os.utime(codex_sess, (old_time, old_time))

        projects, _ = discover_projects(home=tmp_path)
        matching = [p for p in projects if "myapp" in p["path"]]
        assert matching
        assert matching[0]["tool"] == "claude"
