import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from magent.sessions import (
    FLASH_MSG_MAX,
    build_code_open_command,
    build_flash_url,
    build_start_command,
    folder_for_session,
    fresh_start_command,
)
from magent.sessions import claude as claude_sessions
from magent.sessions.claude import (
    claude_fresh_command,
    default_config_dir,
    encode_claude_project_path,
    get_claude_session_ids,
    has_claude_session,
)
from magent.sessions.codex import codex_fresh_command, get_codex_session_ids
from tests.conftest import REAL_HOME


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

    def test_dots_become_dashes(self):
        # Claude Code replaces EVERY non-alphanumeric character; the old rule
        # kept '.', named the wrong directory, and made the fresh-start probe
        # drop --continue for every dotted project.
        assert encode_claude_project_path("my-project.v2") == "my-project-v2"

    def test_underscores_become_dashes(self):
        assert (
            encode_claude_project_path(r"C:\Users\amind\AppData\Local\Temp\capture_cc")
            == "C--Users-amind-AppData-Local-Temp-capture-cc"
        )

    def test_a_dot_directory_becomes_a_double_dash(self):
        assert encode_claude_project_path("/home/amin/.claude") == "-home-amin--claude"

    def test_spaces_become_dashes(self):
        result = encode_claude_project_path("my project")
        assert result == "my-project"

    def test_consecutive_special_chars_not_collapsed(self):
        result = encode_claude_project_path("a&&b")
        assert result == "a--b"


class TestTheEncoderIsClaudeCodesOwnRule:
    """Vectors read off claude.exe's own encoder:
    ``replace(/[^a-zA-Z0-9]/g, "-")`` over UTF-16 code units, the drive
    letter's case kept, and a name over 200 units cut to 200 + "-" +
    base36(|Java String.hashCode of the ORIGINAL path|)."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (
                r"C:\p\stealth-chrome-devtools-mcp\.claude\worktrees\agent-a0ed696fa523ab8f6",
                "C--p-stealth-chrome-devtools-mcp--claude-worktrees-agent-a0ed696fa523ab8f6",
            ),
            (
                r"c:\Users\amind\OneDrive\Desktop\Projects\INTERNAL\devino-landing-page",
                "c--Users-amind-OneDrive-Desktop-Projects-INTERNAL-devino-landing-page",
            ),
            ("/home/amin/magent/my_repo.v2", "-home-amin-magent-my-repo-v2"),
        ],
    )
    def test_every_ascii_character_outside_letters_and_digits_becomes_a_dash(
        self, path, expected
    ):
        assert encode_claude_project_path(path) == expected

    def test_a_non_ascii_character_costs_one_dash_per_utf16_unit(self):
        # é is one UTF-16 unit (one dash); the emoji is a surrogate pair (two).
        # A code-point regex gives 3 trailing dashes here, a byte regex 6.
        assert (
            encode_claude_project_path("/home/amin/café \U0001f600")
            == "-home-amin-caf----"
        )

    def test_a_name_over_200_units_is_cut_and_suffixed_with_the_paths_hash(self):
        # A 250-'a' name: cut to 200 units, then the hash suffix of the whole path.
        encoded = encode_claude_project_path("/home/amin/magent/" + "a" * 250)
        assert encoded == "-home-amin-magent-" + "a" * 182 + "-d43su2"
        assert len(encoded) == 207

    def test_a_name_of_exactly_200_units_is_left_whole(self):
        assert encode_claude_project_path("/" + "b" * 199) == "-" + "b" * 199

    def test_the_cut_suffix_uses_the_hash_of_the_whole_original_path(self):
        assert (
            encode_claude_project_path("/" + "b" * 300) == "-" + "b" * 199 + "-km8bov"
        )

    def test_the_hash_is_javas_string_hash_code(self):
        assert claude_sessions._java_string_hash("hello") == 99162322

    def test_the_most_negative_hash_still_encodes_as_a_positive_number(self):
        # "polygenelubricants".hashCode() is Integer.MIN_VALUE in Java: abs()
        # must happen on the Python int, never wrap back to negative.
        assert claude_sessions._java_string_hash("polygenelubricants") == -(2**31)
        assert claude_sessions._base36(2**31) == "zik0zk"


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
        ids = get_claude_session_ids("test-project", 3, config_dir=home / ".claude")
        assert ids == ["uuid-newest", "uuid-middle", "uuid-oldest"]

    def test_returns_fewer_than_requested(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        encoded = "test-project"
        fake_claude_sessions(encoded, [("uuid-1", 1000.0), ("uuid-2", 2000.0)])
        ids = get_claude_session_ids("test-project", 5, config_dir=home / ".claude")
        assert ids == ["uuid-2", "uuid-1", None, None, None]

    def test_empty_dir(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        fake_claude_sessions("test-project", [])
        ids = get_claude_session_ids("test-project", 3, config_dir=home / ".claude")
        assert ids == [None, None, None]

    def test_no_dir_exists(self, tmp_path):
        ids = get_claude_session_ids("nonexistent", 2, config_dir=tmp_path / ".claude")
        assert ids == [None, None]

    def test_count_one(self, fake_claude_sessions, tmp_path):
        home = tmp_path
        encoded = "test-project"
        fake_claude_sessions(encoded, [("uuid-1", 1000.0), ("uuid-2", 2000.0)])
        ids = get_claude_session_ids("test-project", 1, config_dir=home / ".claude")
        assert ids == ["uuid-2"]


class TestTheDefaultStoreIsTheHomeClaudeDir:
    """Characterization pins for the store each probe reads when the caller
    names none. Written against the redirected HOME (tests/conftest.py's
    autouse isolation), so they assert the DEFAULT resolution -- ``~/.claude``
    for claude, ``~/.codex`` for codex -- rather than whatever seam parameter
    happens to be spelled today. That is exactly the property the config-dir
    rework must not move: an unrouted project keeps reading the same files.
    """

    def _write_claude(self, slug: str, name: str = "uuid-1") -> None:
        sess_dir = Path.home() / ".claude" / "projects" / slug
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / f"{name}.jsonl").write_text('{"type":"message"}\n')

    def test_has_claude_session_reads_home_claude_projects(self):
        assert has_claude_session("/home/user/api") is False
        self._write_claude("-home-user-api")
        assert has_claude_session("/home/user/api") is True

    def test_get_claude_session_ids_reads_home_claude_projects(self):
        assert get_claude_session_ids("/home/user/api", 2) == [None, None]
        self._write_claude("-home-user-api", "uuid-a")
        assert get_claude_session_ids("/home/user/api", 2) == ["uuid-a", None]

    def test_claude_fresh_command_probes_home_claude_projects(self):
        assert claude_fresh_command("claude --continue", "/home/user/api") == "claude"
        self._write_claude("-home-user-api")
        assert claude_fresh_command("claude --continue", "/home/user/api") is None

    def test_build_start_command_drops_continue_off_the_default_store(self):
        assert (
            build_start_command("claude", "claude --continue", "/home/user/api")
            == "claude"
        )
        self._write_claude("-home-user-api")
        assert (
            build_start_command("claude", "claude --continue", "/home/user/api")
            == "claude --continue"
        )

    def test_get_codex_session_ids_reads_home_codex_sessions(self):
        assert get_codex_session_ids("/home/user/api", 1) == [None]
        day = Path.home() / ".codex" / "sessions" / "2026" / "06" / "20"
        day.mkdir(parents=True, exist_ok=True)
        (day / "session-0-uuid-c.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "uuid-c", "cwd": "/home/user/api"},
                }
            )
            + "\n"
        )
        assert get_codex_session_ids("/home/user/api", 1) == ["uuid-c"]


class TestANamedConfigDirAnswersForTheProject:
    """The account-routing half: under ``CLAUDE_CONFIG_DIR=<profile>`` claude
    writes ``<profile>/projects/<encoded cwd>``, so a probe told which store to
    read must read THAT one -- and must keep answering out of ``~/.claude``
    when told nothing."""

    def _write(self, root: Path, slug: str, name: str = "uuid-1") -> None:
        sess_dir = root / "projects" / slug
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / f"{name}.jsonl").write_text('{"type":"message"}\n')

    def test_the_named_store_is_read_and_the_home_one_is_not(self, tmp_path):
        profile = tmp_path / "sessions" / "13-acct"
        self._write(profile, "-home-user-api", "uuid-routed")
        # The default store has a DIFFERENT conversation for the same project.
        self._write(Path.home() / ".claude", "-home-user-api", "uuid-default")

        assert get_claude_session_ids("/home/user/api", 1, profile) == ["uuid-routed"]
        assert get_claude_session_ids("/home/user/api", 1) == ["uuid-default"]
        assert has_claude_session("/home/user/api", profile) is True

    def test_a_project_with_no_history_in_that_store_starts_fresh(self, tmp_path):
        # The move case: the conversation exists on the OLD account only, so
        # the new account's store honestly answers "nothing to continue".
        self._write(Path.home() / ".claude", "-home-user-api")
        profile = tmp_path / "sessions" / "19-acct"
        profile.mkdir(parents=True)

        assert has_claude_session("/home/user/api", profile) is False
        assert claude_fresh_command("claude --continue", "/home/user/api") is None
        assert (
            claude_fresh_command("claude --continue", "/home/user/api", profile)
            == "claude"
        )

    def test_build_start_command_threads_the_config_dir_through(self, tmp_path):
        profile = tmp_path / "sessions" / "13-acct"
        self._write(profile, "-home-user-api")

        assert (
            build_start_command(
                "claude", "claude --continue", "/home/user/api", config_dir=profile
            )
            == "claude --continue"
        )
        # Same project, same command, a store with no transcript for it.
        assert (
            build_start_command(
                "claude",
                "claude --continue",
                "/home/user/api",
                config_dir=tmp_path / "sessions" / "19-acct",
            )
            == "claude"
        )

    def test_default_config_dir_is_resolved_at_call_time(self, tmp_path, monkeypatch):
        # Never an import-bound constant: the value has to follow a redirected
        # home, which is what keeps the test tripwire meaningful.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_config_dir() == tmp_path / ".claude"


class TestHasClaudeSession:
    """The "is this a brand-new project directory?" probe. Existence only --
    it must not care which session is newest, or what is inside the file."""

    def test_true_when_a_session_file_exists(self, fake_claude_sessions, tmp_path):
        fake_claude_sessions("test-project", [("uuid-1", 1000.0)])
        assert has_claude_session("test-project", tmp_path / ".claude") is True

    def test_false_for_a_directory_with_no_session_files(
        self, fake_claude_sessions, tmp_path
    ):
        fake_claude_sessions("test-project", [])
        assert has_claude_session("test-project", tmp_path / ".claude") is False

    def test_false_when_the_project_was_never_opened(self, tmp_path):
        # The headline case: a project just added to magent, or a fresh
        # machine -- <config dir>/projects/<encoded> does not exist at all.
        assert has_claude_session("nonexistent", tmp_path / ".claude") is False

    def test_the_project_path_is_encoded_like_claude_encodes_it(self, tmp_path):
        sess_dir = tmp_path / ".claude" / "projects" / "-home-user-api"
        sess_dir.mkdir(parents=True)
        (sess_dir / "uuid-1.jsonl").write_text("{}\n")
        assert has_claude_session("/home/user/api", tmp_path / ".claude") is True

    def test_non_jsonl_files_do_not_count(self, tmp_path):
        sess_dir = tmp_path / ".claude" / "projects" / "test-project"
        sess_dir.mkdir(parents=True)
        (sess_dir / "notes.txt").write_text("hi")
        assert has_claude_session("test-project", tmp_path / ".claude") is False


class TestClaudeFreshCommand:
    """`claude --continue` in a directory with no stored conversation errors
    out and leaves a dead pane. These pin exactly when the flag is dropped --
    and, just as importantly, when it is kept so the failure stays visible."""

    def _fresh(self, cmd, tmp_path, project="test-project"):
        return claude_fresh_command(cmd, project, tmp_path / ".claude")

    def test_new_directory_drops_the_continue_flag(self, tmp_path):
        assert self._fresh("claude --continue", tmp_path) == "claude"

    def test_the_short_flag_is_deliberately_not_matched(self, tmp_path):
        # `-c` is claude's own alias for --continue, but the same token belongs
        # to a WRAPPER in `bash -c claude ...`, and corrupting a working command
        # is worse than leaving the rarer spelling on its old behavior.
        assert self._fresh("claude -c", tmp_path) is None

    def test_an_interpreter_wrapped_command_is_not_corrupted(self, tmp_path):
        # The wrapper's own -c survives; only claude's long flag goes.
        assert self._fresh("bash -c claude --continue", tmp_path) == "bash -c claude"

    def test_a_quoted_wrapper_payload_is_left_alone(self, tmp_path):
        # --continue is followed by a quote, not whitespace: no token match, so
        # magent does not try to rewrite inside someone else's argument.
        assert self._fresh('bash -c "claude --continue"', tmp_path) is None

    def test_other_arguments_are_preserved(self, tmp_path):
        assert (
            self._fresh("claude --continue --dangerously-skip-permissions", tmp_path)
            == "claude --dangerously-skip-permissions"
        )
        assert (
            self._fresh("claude --model opus --continue", tmp_path)
            == "claude --model opus"
        )

    def test_existing_session_keeps_the_command_untouched(
        self, fake_claude_sessions, tmp_path
    ):
        fake_claude_sessions("test-project", [("uuid-1", 1000.0)])
        assert self._fresh("claude --continue", tmp_path) is None

    def test_an_empty_session_file_still_counts_as_a_session(self, tmp_path):
        # Deliberate posture: a session file that exists but is empty or
        # corrupt keeps --continue. That failure is a real defect the user
        # needs to SEE in the pane, not one to paper over with a fresh chat.
        sess_dir = tmp_path / ".claude" / "projects" / "test-project"
        sess_dir.mkdir(parents=True)
        (sess_dir / "uuid-1.jsonl").write_text("")
        assert self._fresh("claude --continue", tmp_path) is None

    def test_an_explicitly_named_session_is_never_rewritten(self, tmp_path):
        # The user spelled out which conversation they want; a new-directory
        # probe has no business overriding that.
        assert self._fresh("claude --resume abc123", tmp_path) is None
        assert self._fresh("claude --resume=abc123", tmp_path) is None
        assert self._fresh("claude -r abc123", tmp_path) is None

    def test_the_interactive_resume_picker_is_never_rewritten(self, tmp_path):
        assert self._fresh("claude --resume", tmp_path) is None

    def test_a_command_that_asks_for_both_is_left_alone(self, tmp_path):
        assert self._fresh("claude --continue --resume abc123", tmp_path) is None

    def test_a_command_with_no_resume_flag_is_left_alone(self, tmp_path):
        assert self._fresh("claude", tmp_path) is None
        assert self._fresh("claude --model opus", tmp_path) is None

    def test_a_longer_flag_that_merely_starts_the_same_is_not_matched(self, tmp_path):
        assert self._fresh("claude --continue-on-error", tmp_path) is None
        assert self._fresh("claude --config x", tmp_path) is None


class TestCodexFreshCommand:
    """codex's symmetric case. Its resume form is the explicit subcommand
    `codex resume <id>`, so the default `codex` has nothing to rewrite -- the
    only hazardous shape is a hand-configured `codex resume --last`."""

    def test_the_registry_default_is_never_rewritten(self, tmp_path):
        assert (
            codex_fresh_command("codex", "/home/user/api", home_override=tmp_path)
            is None
        )

    def test_resume_last_in_a_new_directory_drops_back_to_the_binary(self, tmp_path):
        assert (
            codex_fresh_command(
                "codex resume --last", "/home/user/api", home_override=tmp_path
            )
            == "codex"
        )

    def test_resume_last_with_a_stored_session_is_left_alone(
        self, fake_codex_sessions, tmp_path
    ):
        fake_codex_sessions([("/home/user/api", "uuid-1", 1000.0)])
        assert (
            codex_fresh_command(
                "codex resume --last", "/home/user/api", home_override=tmp_path
            )
            is None
        )

    def test_an_explicitly_named_session_is_never_rewritten(self, tmp_path):
        assert (
            codex_fresh_command(
                "codex resume uuid-1", "/home/user/api", home_override=tmp_path
            )
            is None
        )

    def test_a_claude_config_dir_is_accepted_and_ignored(
        self, fake_codex_sessions, tmp_path
    ):
        """codex's store is ~/.codex, one per machine and not account-scoped:
        the registry's config_dir argument must change nothing here."""
        fake_codex_sessions([("/home/user/api", "uuid-1", 1000.0)])
        assert (
            codex_fresh_command(
                "codex resume --last",
                "/home/user/api",
                tmp_path / "profile-13",
                home_override=tmp_path,
            )
            is None
        )
        assert get_codex_session_ids(
            "/home/user/api", 1, tmp_path / "profile-13", home_override=tmp_path
        ) == ["uuid-1"]


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


def _first_cwd(transcript: Path) -> str | None:
    with transcript.open(encoding="utf-8", errors="replace") as f:
        for _, line in zip(range(50), f, strict=False):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                return record["cwd"]
    return None


def _real_project_dirs(limit: int = 400) -> list[tuple[Path, str]]:
    """(directory, recorded cwd) for the real ~/.claude/projects -- READ ONLY.
    Empty where there is no store (CI)."""
    store = REAL_HOME / ".claude" / "projects"
    if not store.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for directory in sorted(store.iterdir())[:limit]:
        if not directory.is_dir():
            continue
        for transcript in sorted(directory.glob("*.jsonl"))[:1]:
            cwd = _first_cwd(transcript)
            if cwd:
                out.append((directory, cwd))
    return out


class TestTheEncoderMatchesClaudeCodesOwnStore:
    """The encoder is only right if it names the directory the CLI actually
    wrote. These read the REAL store, read-only, and skip where there is none.

    The evidence behind the rule (2026-09-24, one developer machine): of 297
    ~/.claude/projects dirs with a recorded cwd, 295 are named exactly
    re.sub("[^A-Za-z0-9]", "-", cwd) (drive-letter case aside), and all 83
    cwds containing '_' were encoded '-'. The old rule, [^a-zA-Z0-9._-], kept
    '.' and '_' and so named the wrong directory for every such project. The
    2 misses were sessions that cd'd into a worktree mid-run -- the new cwd is
    recorded in a transcript filed under the old directory -- so a few misses
    are tolerated here, never a systematic one."""

    def test_this_checkouts_entry_is_named_by_the_encoder(self):
        repo = str(Path(__file__).resolve().parents[2])
        entry = next(
            (
                d
                for d, cwd in _real_project_dirs()
                if os.path.normcase(cwd) == os.path.normcase(repo)
            ),
            None,
        )
        if entry is None:
            pytest.skip("no Claude Code session has run in this checkout")
        assert encode_claude_project_path(repo).lower() == entry.name.lower()

    def test_dotted_and_underscored_projects_match_the_store(self):
        pairs = [
            (d, cwd) for d, cwd in _real_project_dirs() if "." in cwd or "_" in cwd
        ]
        if not pairs:
            pytest.skip("no dotted or underscored project in the real store")
        wrong = [
            (d.name, cwd)
            for d, cwd in pairs
            if encode_claude_project_path(cwd).lower() != d.name.lower()
        ]
        assert len(wrong) <= max(1, len(pairs) // 10), wrong[:5]


class TestAFreshFormNeedsNoStore:
    """On a node, whether a transcript exists is the NODE's question
    (bring_up.sh answers it); the PC only supplies both commands."""

    def test_claude_drops_continue_and_keeps_every_other_flag(self):
        assert fresh_start_command("claude", "claude --continue --model opus") == (
            "claude --model opus"
        )

    def test_an_explicit_resume_has_no_fresh_form(self):
        assert fresh_start_command("claude", "claude --resume abc") is None

    def test_a_command_that_never_resumes_has_no_fresh_form(self):
        assert fresh_start_command("claude", "claude --model opus") is None

    def test_the_fresh_form_never_reads_a_store(self, monkeypatch):
        def boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("fresh_form must not probe a transcript store")

        monkeypatch.setattr("magent.sessions.claude.has_claude_session", boom)
        assert fresh_start_command("claude", "claude --continue") == "claude"

    def test_codex_drops_resume_last_without_reading_its_store(self, monkeypatch):
        def boom(*_a: object, **_k: object) -> list[str | None]:
            raise AssertionError("fresh_form must not probe codex's session store")

        monkeypatch.setattr("magent.sessions.codex.get_codex_session_ids", boom)
        assert fresh_start_command("codex", "codex resume --last") == "codex"

    @pytest.mark.parametrize("cmd", ["codex", "codex resume uuid-1"])
    def test_a_codex_command_with_no_implicit_resume_has_no_fresh_form(self, cmd):
        assert fresh_start_command("codex", cmd) is None

    def test_an_unknown_tool_has_none(self):
        assert fresh_start_command("aider", "aider --continue") is None

    def test_the_local_fresh_command_still_probes(self, monkeypatch):
        # The refactor keeps build_start_command's verdict: a session here
        # keeps --continue.
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session", lambda *_a: True
        )

        assert claude_fresh_command("claude --continue", "/p") is None


class TestRemoteSshCanKeepTheUser:
    def test_by_default_the_user_is_stripped(self):
        assert build_code_open_command("/f", "amin@devino-second", "code") == [
            "code",
            "--remote",
            "ssh-remote+devino-second",
            "/f",
        ]

    def test_a_node_keeps_the_user_magent_resolved(self):
        # D4: the node user may exist only in magent's config, never in
        # ~/.ssh/config, so the authority has to carry it.
        assert build_code_open_command(
            "/home/amin/magent/api", "amin@devino-second", "code", keep_user=True
        ) == [
            "code",
            "--remote",
            "ssh-remote+amin@devino-second",
            "/home/amin/magent/api",
        ]

    def test_a_user_only_target_still_opens_locally_when_keeping_the_user(self):
        # `amin@` names no host; `ssh-remote+amin@` would be a broken URI.
        assert build_code_open_command("/f", "amin@", "code", keep_user=True) == [
            "code",
            "/f",
        ]
