import os
import sys
from urllib.parse import parse_qs, urlparse

from magent.sessions import (
    FLASH_MSG_MAX,
    build_code_open_command,
    build_flash_url,
    folder_for_session,
)
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


class TestBuildCodeOpenCommand:
    """argv for the F2 "open this project in VS Code" hotkey. Pure and
    win32-free on purpose: hotkey.py raises ImportError off Windows, so the
    decision logic lives here where every OS in the matrix can test it."""

    def test_local_open_is_bin_plus_folder(self):
        assert build_code_open_command("/a/api", None, "code") == ["code", "/a/api"]

    def test_remote_open_uses_ssh_remote_authority(self):
        assert build_code_open_command("/a/api", "host", "code") == [
            "code",
            "--remote",
            "ssh-remote+host",
            "/a/api",
        ]

    def test_user_prefix_is_stripped_from_the_authority(self):
        # VS Code resolves the login user from the machine's ssh config; the
        # attach target is user@host, so only the hostname goes into the URI.
        assert build_code_open_command("/a/api", "amin@deck", "code") == [
            "code",
            "--remote",
            "ssh-remote+deck",
            "/a/api",
        ]

    def test_empty_ssh_host_degrades_to_a_local_open(self):
        assert build_code_open_command("/a/api", "", "code") == ["code", "/a/api"]
        assert build_code_open_command("/a/api", "amin@", "code") == ["code", "/a/api"]

    def test_resolved_code_binary_is_used_verbatim(self):
        # shutil.which resolves code.cmd on Windows; Popen runs it directly.
        argv = build_code_open_command(r"C:\a\api", None, r"C:\bin\code.cmd")
        assert argv == [r"C:\bin\code.cmd", r"C:\a\api"]


class TestFolderForSession:
    """Picking the folder to open out of an /api/sessions response body."""

    def _payload(self, *entries):
        return {"ok": True, "sessions": list(entries)}

    def test_prefers_resolved_over_raw_path(self):
        # `path` is the raw config value and may be relative to the host's
        # baseDir -- meaningless to the client doing the opening.
        payload = self._payload(
            {
                "name": "caly",
                "session": "caly",
                "path": "INTERNAL/caly",
                "resolved": "/base/INTERNAL/caly",
            }
        )
        assert folder_for_session(payload, "caly") == "/base/INTERNAL/caly"

    def test_falls_back_to_path_when_resolved_is_empty(self):
        payload = self._payload(
            {"name": "caly", "session": "caly", "path": "/abs/caly", "resolved": ""}
        )
        assert folder_for_session(payload, "caly") == "/abs/caly"

    def test_matches_on_the_display_name_too(self):
        # Window titles carry the psmux socket id, but a display name must
        # still resolve -- the two differ whenever the title has dots/spaces.
        payload = self._payload(
            {"name": "my.api", "session": "my-api", "resolved": "/a/my.api"}
        )
        assert folder_for_session(payload, "my-api") == "/a/my.api"
        assert folder_for_session(payload, "my.api") == "/a/my.api"

    def test_missing_project_is_none(self):
        payload = self._payload({"name": "caly", "session": "caly", "path": "/a/caly"})
        assert folder_for_session(payload, "ghost") is None

    def test_entry_without_any_folder_is_none(self):
        payload = self._payload({"name": "caly", "session": "caly", "resolved": ""})
        assert folder_for_session(payload, "caly") is None

    def test_wrong_shaped_payloads_are_none(self):
        assert folder_for_session(None, "caly") is None
        assert folder_for_session([], "caly") is None
        assert folder_for_session({"ok": False}, "caly") is None
        assert folder_for_session({"sessions": "nope"}, "caly") is None
        assert folder_for_session({"sessions": ["nope"]}, "caly") is None


class TestBuildFlashUrl:
    """The URL the hidden F2 listener uses to say something on screen. Pure
    string math, tested on every OS for the same reason as the argv builder
    above -- hotkey.py, its only caller, is win32-import-only."""

    def _query(self, url: str) -> dict[str, list[str]]:
        return parse_qs(urlparse(url).query)

    def test_hits_the_flash_route_with_both_params(self):
        url = build_flash_url("http://127.0.0.1:8033", "caly", "F2: opening VS Code...")
        assert url.startswith("http://127.0.0.1:8033/api/flash?")
        assert self._query(url) == {
            "project": ["caly"],
            "msg": ["F2: opening VS Code..."],
        }

    def test_trailing_slash_on_the_server_url_is_not_doubled(self):
        url = build_flash_url("http://127.0.0.1:8033/", "caly", "hi")
        assert "//api/flash" not in url
        assert url.startswith("http://127.0.0.1:8033/api/flash?")

    def test_special_characters_survive_the_round_trip(self):
        # Windows paths (backslashes, colons, spaces) and the "&"/"?" that
        # would otherwise split the query string.
        msg = r"F2: VS Code -> C:\Users\a b\my api & co?x"
        url = build_flash_url("http://h:8033", "my project", msg)
        q = self._query(url)
        assert q["project"] == ["my project"]
        assert q["msg"] == [msg]

    def test_long_messages_are_clamped_to_the_shared_budget(self):
        url = build_flash_url("http://h:8033", "caly", "z" * (FLASH_MSG_MAX + 50))
        assert self._query(url)["msg"] == ["z" * FLASH_MSG_MAX]
