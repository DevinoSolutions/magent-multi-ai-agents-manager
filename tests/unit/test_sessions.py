import os
import sys

from magent.sessions.claude import encode_claude_project_path, get_claude_session_ids
from magent.sessions.codex import get_codex_session_ids


class TestEncodeClaudeProjectPath:
    def test_windows_path(self):
        result = encode_claude_project_path(
            r"C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\magent-multi-ai-agents-manager"
        )
        assert (
            result
            == "C--Users-amind-OneDrive-Desktop-Projects-CUSTOM-MCPs---PRODUCTIVITY-magent-multi-ai-agents-manager"
        )

    def test_unix_path(self):
        result = encode_claude_project_path("/home/user/code/my-project")
        assert result == "-home-user-code-my-project"

    def test_preserves_dots_and_dashes(self):
        result = encode_claude_project_path("my-project.v2")
        assert result == "my-project.v2"

    def test_spaces_become_dashes(self):
        result = encode_claude_project_path("my project")
        assert result == "my-project"

    def test_consecutive_special_chars_not_collapsed(self):
        result = encode_claude_project_path("a&&b")
        assert result == "a--b"


class TestGetClaudeSessionIds:
    def test_returns_ids_sorted_by_mtime(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        encoded = "test-project"
        fake_claude_sessions(
            encoded,
            [
                ("uuid-oldest", 1000.0),
                ("uuid-newest", 3000.0),
                ("uuid-middle", 2000.0),
            ],
        )
        ids = get_claude_session_ids("test-project", 3, home_override=home)
        assert ids == ["uuid-newest", "uuid-middle", "uuid-oldest"]

    def test_returns_fewer_than_requested(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        encoded = "test-project"
        fake_claude_sessions(encoded, [("uuid-1", 1000.0), ("uuid-2", 2000.0)])
        ids = get_claude_session_ids("test-project", 5, home_override=home)
        assert ids == ["uuid-2", "uuid-1", None, None, None]

    def test_empty_dir(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        fake_claude_sessions("test-project", [])
        ids = get_claude_session_ids("test-project", 3, home_override=home)
        assert ids == [None, None, None]

    def test_no_dir_exists(self, tmp_path):
        ids = get_claude_session_ids("nonexistent", 2, home_override=tmp_path)
        assert ids == [None, None]

    def test_count_one(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        encoded = "test-project"
        fake_claude_sessions(encoded, [("uuid-1", 1000.0), ("uuid-2", 2000.0)])
        ids = get_claude_session_ids("test-project", 1, home_override=home)
        assert ids == ["uuid-2"]


class TestGetCodexSessionIds:
    def test_returns_matching_sessions_sorted_by_mtime(
        self, fake_codex_sessions, tmp_path
    ):
        fake_codex_sessions(
            [
                ("/home/user/api", "uuid-oldest", 1000.0),
                ("/home/user/api", "uuid-newest", 3000.0),
                ("/home/user/other", "uuid-other", 2000.0),
                ("/home/user/api", "uuid-middle", 2000.0),
            ]
        )
        home = tmp_path
        ids = get_codex_session_ids("/home/user/api", 3, home_override=home)
        assert ids == ["uuid-newest", "uuid-middle", "uuid-oldest"]

    def test_case_insensitive_on_windows(
        self, fake_codex_sessions, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_codex_sessions(
            [
                ("C:\\Users\\User\\api", "uuid-1", 1000.0),
            ]
        )
        home = tmp_path
        ids = get_codex_session_ids("c:\\users\\user\\api", 1, home_override=home)
        assert ids == ["uuid-1"]

    def test_fewer_than_requested(self, fake_codex_sessions, tmp_path):
        fake_codex_sessions(
            [
                ("/home/user/api", "uuid-1", 1000.0),
            ]
        )
        home = tmp_path
        ids = get_codex_session_ids("/home/user/api", 3, home_override=home)
        assert ids == ["uuid-1", None, None]

    def test_no_matching_sessions(self, fake_codex_sessions, tmp_path):
        fake_codex_sessions(
            [
                ("/home/user/other", "uuid-1", 1000.0),
            ]
        )
        home = tmp_path
        ids = get_codex_session_ids("/home/user/api", 2, home_override=home)
        assert ids == [None, None]

    def test_no_sessions_dir(self, tmp_path):
        ids = get_codex_session_ids("/any", 2, home_override=tmp_path)
        assert ids == [None, None]

    def test_malformed_jsonl_skipped(self, fake_codex_sessions, tmp_path):
        fake_codex_sessions(
            [
                ("/home/user/api", "uuid-good", 2000.0),
            ]
        )
        bad_dir = tmp_path / ".codex" / "sessions" / "2026" / "06" / "30"
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad_file = bad_dir / "bad.jsonl"
        bad_file.write_text("not json\n")
        os.utime(bad_file, (3000.0, 3000.0))
        ids = get_codex_session_ids("/home/user/api", 2, home_override=tmp_path)
        assert ids[0] == "uuid-good"
        assert ids[1] is None
