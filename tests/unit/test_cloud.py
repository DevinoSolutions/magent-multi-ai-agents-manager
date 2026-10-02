"""The cloud backend (sub-plan J, spec §18): a LOCAL psmux pane running
`claude --cloud`, created once, only from a clean pushed GitHub checkout, and
only after the push set was sealed or handed off."""

from __future__ import annotations

import dataclasses
import sys
from typing import TYPE_CHECKING

import pytest

from magent import nodes
from magent.config import SCHEMA_VERSION, ConfigError, load_config
from magent.nodes import LocalGitState

if TYPE_CHECKING:
    from pathlib import Path


def _cloud_cfg(tmp_config, tmp_path: Path, **proj: object) -> str:
    project_dir = tmp_path / "api"
    project_dir.mkdir(exist_ok=True)
    entry: dict[str, object] = {
        "path": str(project_dir),
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    entry.update(proj)
    return tmp_config({"version": SCHEMA_VERSION, "projects": [entry]})


class TestACloudProjectIsValidatedAtLoad:
    def test_a_cloud_project_with_a_task_loads(self, tmp_config, tmp_path):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        assert cfg.projects[0].cloud_task == "Fix the login bug"

    def test_a_cloud_project_without_a_task_still_loads_silently(
        self, tmp_config, tmp_path, capsys
    ):
        # The nodes release already accepted `"node": "cloud"` with no task, so
        # a required field would break configs users have. The create gate (J7)
        # is what refuses it.
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=None))
        assert cfg.projects[0].cloud_task is None
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("task", [5, ["a", "b"], True])
    def test_a_non_string_task_is_treated_as_missing(self, tmp_config, tmp_path, task):
        # Exactly like every other string field (`_str_or_none`).
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))
        assert cfg.projects[0].cloud_task is None

    @pytest.mark.parametrize(
        "task",
        [
            'say "hi"',
            "cost $5",
            "a & b",
            "100%",
            "a`b",
            "a; b",
            "a | b",
            "a < b",
            "wow!",
            "(x)",
            "#tag",
            "it's",
            "-p leak",
            "",
        ],
    )
    def test_a_task_carrying_a_shell_metacharacter_or_a_flag_is_refused(
        self, tmp_config, tmp_path, task
    ):
        # The text crosses pwsh AND cmd.exe (psmux types `cmd /c <command>`),
        # so the charset admits no metacharacter of either, and no leading '-'.
        with pytest.raises(ConfigError, match=r"cloudTask must be"):
            load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))

    @pytest.mark.parametrize(
        "task",
        [
            "a\nb",  # a line break mid-text would end the typed command
            "abc\n",  # ... and a trailing one must not slip past a `$` anchor
            "a\r",
            "a\tb",
            "caf" + chr(0xE9),  # the charset is ASCII: a non-ASCII letter is refused
            chr(0xFF41),  # full-width "a" looks like a letter and is not one
        ],
    )
    def test_a_control_character_or_a_non_ascii_letter_is_refused(
        self, tmp_config, tmp_path, task
    ):
        # Pins the match as a FULL match over an ASCII charset: a switch to
        # `re.match(... $)` (which lets a trailing newline through) or to `\w`
        # (which admits Unicode letters) fails here.
        with pytest.raises(ConfigError, match=r"cloudTask must be"):
            load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))

    @pytest.mark.parametrize("task", ["-", " a", ".a", "_a", "/a", ":a", ",a"])
    def test_a_task_must_start_with_a_letter_or_a_digit(
        self, tmp_config, tmp_path, task
    ):
        # By design (a leading '-' reads as a flag); the others are refused
        # with it rather than guessed at.
        with pytest.raises(ConfigError, match=r"cloudTask must be"):
            load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))

    @pytest.mark.parametrize(
        "task",
        [
            "Fix a/b: x, y.z_w-1",  # one of everything the charset admits
            "a",  # the minimum length
            "7",  # a digit may start a task
            "a--b",  # '-' is only barred at the start
            "a ",  # trailing space is plain charset
            "Refactor the session_store module",  # mentions an id prefix mid-text
        ],
    )
    def test_the_whole_allowed_charset_loads(self, tmp_config, tmp_path, task):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))
        assert cfg.projects[0].cloud_task == task

    @pytest.mark.parametrize(
        "task", ["session_01ABCdef", "cse_9zz", "https://claude.ai/code/session_1"]
    )
    def test_a_task_that_looks_like_a_session_id_or_url_is_refused(
        self, tmp_config, tmp_path, task
    ):
        # `claude --cloud <id|url>` ATTACHES instead of creating (claude --help).
        with pytest.raises(ConfigError, match="session id or URL"):
            load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=task))

    def test_a_task_of_two_hundred_characters_is_the_ceiling(
        self, tmp_config, tmp_path
    ):
        load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask="a" * 200))
        with pytest.raises(ConfigError, match=r"cloudTask must be"):
            load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask="a" * 201))

    def test_a_cloud_project_with_another_tool_still_loads_and_warns(
        self, tmp_config, tmp_path, capsys
    ):
        # Never break a config that already loads: one such project would
        # otherwise fail every command. J7's create gate refuses it instead.
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, tool="codex"))
        assert cfg.projects[0].tool == "codex"
        err = capsys.readouterr().err
        assert "projects[0] is a cloud project but its tool is 'codex'" in err
        assert (
            "a cloud project runs claude --cloud, and magent will refuse to "
            "create it until its tool is claude"
        ) in err

    def test_a_cloud_project_inheriting_another_default_tool_warns(
        self, tmp_config, tmp_path, capsys
    ):
        project_dir = tmp_path / "api"
        project_dir.mkdir(exist_ok=True)
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"defaultTool": "codex"},
                "projects": [{"path": str(project_dir), "node": "cloud"}],
            }
        )
        cfg = load_config(path)
        assert cfg.projects[0].tool is None
        assert "projects[0] is a cloud project but its tool is 'codex'" in (
            capsys.readouterr().err
        )

    def test_a_cloud_project_running_claude_does_not_warn_about_its_tool(
        self, tmp_config, tmp_path, capsys
    ):
        load_config(_cloud_cfg(tmp_config, tmp_path, tool="claude"))
        assert capsys.readouterr().err == ""

    def test_a_bad_task_is_still_refused_on_another_tool(self, tmp_config, tmp_path):
        # The tool is a warning; the shell-unsafe task stays a hard error.
        with pytest.raises(ConfigError, match=r"cloudTask must be"):
            load_config(_cloud_cfg(tmp_config, tmp_path, tool="codex", cloudTask="a;b"))

    def test_windows_and_happy_on_a_cloud_project_warn_and_are_ignored(
        self, tmp_config, tmp_path, capsys
    ):
        load_config(_cloud_cfg(tmp_config, tmp_path, windows=2, happy=True))
        err = capsys.readouterr().err
        assert "projects[0].windows is ignored on a cloud project" in err
        assert "projects[0].happy is ignored on a cloud project" in err

    def test_an_explicit_happy_false_on_a_cloud_project_is_a_no_op_and_silent(
        self, tmp_config, tmp_path, capsys
    ):
        load_config(_cloud_cfg(tmp_config, tmp_path, happy=False))
        assert capsys.readouterr().err == ""

    def test_a_cloud_task_on_a_project_that_is_not_cloud_warns(
        self, tmp_config, tmp_path, capsys
    ):
        # `"node": null` is itself refused at load, so the key is dropped, not
        # nulled: the project is a plain local one that carries a stray task.
        project_dir = tmp_path / "api"
        project_dir.mkdir(exist_ok=True)
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [{"path": str(project_dir), "cloudTask": "Fix it"}],
            }
        )
        load_config(path)
        assert "projects[0].cloudTask is ignored" in capsys.readouterr().err

    def test_cloud_task_is_a_known_project_key(self, tmp_config, tmp_path, capsys):
        # Not an "unknown key" warning: the key is part of the schema.
        load_config(_cloud_cfg(tmp_config, tmp_path))
        assert "unknown" not in capsys.readouterr().err.lower()


class TestTheThreeKindsOfProject:
    def test_is_cloud_and_runs_on_node_never_both_answer_yes(self):
        from magent.config import ProjectConfig, is_cloud, runs_on_node

        local = ProjectConfig(path="a")
        pooled = ProjectConfig(path="a", node="second")
        placed = ProjectConfig(path="a", node="auto")
        cloud = ProjectConfig(path="a", node="cloud", cloud_task="t")
        assert [is_cloud(p) for p in (local, pooled, placed, cloud)] == [
            False,
            False,
            False,
            True,
        ]
        assert [runs_on_node(p) for p in (local, pooled, placed, cloud)] == [
            False,
            True,
            True,
            False,
        ]


def test_the_config_reference_documents_cloud_task():
    from magent.cli.docs import _PROJECT_FIELD_DOCS

    assert "cloudTask" in {row[0] for row in _PROJECT_FIELD_DOCS}


def test_the_cloud_task_row_says_a_cloud_project_runs_claude():
    from magent.cli.docs import _PROJECT_FIELD_DOCS

    row = next(row for row in _PROJECT_FIELD_DOCS if row[0] == "cloudTask")
    assert "claude" in row[3]


def test_the_node_row_no_longer_calls_cloud_reserved():
    # The sentence was true until J1; "cloud" now runs a Claude cloud session.
    from magent.cli.docs import _PROJECT_FIELD_DOCS

    node_row = next(row for row in _PROJECT_FIELD_DOCS if row[0] == "node")
    assert "reserved in this release" not in node_row[3]
    assert "cloudTask" in node_row[3]


class TestTheCloudPaneCommand:
    def test_it_is_the_configured_claude_executable_with_the_quoted_task(self):
        from magent.sessions.claude import cloud_pane_command

        assert cloud_pane_command("claude --continue", "Fix the login bug") == (
            'claude --cloud "Fix the login bug"'
        )

    def test_it_keeps_a_path_to_the_executable(self):
        from magent.sessions.claude import cloud_pane_command

        assert cloud_pane_command(r"C:\tools\claude.exe --continue", "t") == (
            r'C:\tools\claude.exe --cloud "t"'
        )

    def test_it_drops_every_other_configured_flag(self):
        # --continue/--resume make no sense for a NEW cloud session.
        from magent.sessions.claude import cloud_pane_command

        assert (
            cloud_pane_command("claude --continue --model opus", "t")
            == 'claude --cloud "t"'
        )

    @pytest.mark.parametrize(
        "base",
        [
            "",
            r'"C:\Program Files\claude.exe"',
            "cl&aude",
            # a flag is not an executable: it would be typed as `--continue --cloud "t"`
            "--continue",
            "-c",
        ],
    )
    def test_an_executable_it_cannot_type_safely_is_refused(self, base):
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError):
            cloud_pane_command(base, "t")

    @pytest.mark.parametrize(
        "task", ["session_abc", "cse_abc", "https://claude.ai/code/session_1"]
    )
    def test_a_task_that_would_attach_instead_of_create_is_refused(self, task):
        # `claude --cloud <id|url>` ATTACHES. These pass the charset rule, so
        # only the CLOUD_ID_LIKE check stands between them and the pane.
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError, match="unsafe cloud task"):
            cloud_pane_command("claude", task)

    def test_an_unsafe_task_is_refused_even_past_config_validation(self):
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError):
            cloud_pane_command("claude", 'x" & del *')


def _state(repo: Path, **kw: object) -> LocalGitState:
    # dataclasses.replace takes the overrides as keywords: no suppression needed.
    base = LocalGitState(
        path=repo,
        url="https://github.com/me/api.git",
        branch="main",
        dirty=False,
        unpushed=False,
        detached=False,
        ignored=(),
    )
    return dataclasses.replace(base, **kw)


@pytest.fixture
def cloud_home(tmp_path, monkeypatch) -> Path:
    """NODES_DIR in tmp (tests/conftest.py already redirects the import-bound
    constant; this pins it to a path the test can read back). The digest key,
    the records and the recipient live under ``NODES_DIR/cloud/`` with the
    ``cloud-`` names."""
    monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
    return tmp_path / "nodes" / "cloud"


class TestTheCloudRefusesACheckoutItCouldNotCloneOrPushBack:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/me/api.git",
            "https://me@github.com/me/api.git",
            "git@github.com:me/api.git",
            "ssh://git@github.com/me/api.git",
            "ssh://git@github.com:22/me/api.git",
        ],
    )
    def test_a_clean_pushed_github_branch_is_accepted(self, tmp_path, url):
        assert nodes.cloud_git_refusal(_state(tmp_path, url=url)) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/me/api.git",
            "https://github.com.evil.example/me/api.git",
            "https://gitlab.com/github.com-x/api.git",
            "git@github.com.evil.example:me/api.git",
            "https://evil.example/github.com/me/api.git",
        ],
    )
    def test_the_host_is_parsed_not_substring_matched(self, tmp_path, url):
        refusal = nodes.cloud_git_refusal(_state(tmp_path, url=url))
        assert refusal is not None and "not on GitHub" in refusal

    @pytest.mark.parametrize(
        ("kw", "phrase"),
        [
            ({"url": ""}, "no 'origin' remote"),
            ({"url": "   "}, "no 'origin' remote"),
            ({"detached": True}, "detached"),
            ({"branch": ""}, "detached"),
            ({"no_commits": True}, "no commits"),
            ({"dirty": True}, "uncommitted"),
            ({"unpushed": True}, "unpushed"),
        ],
    )
    def test_each_refusal_names_its_reason(self, tmp_path, kw, phrase):
        refusal = nodes.cloud_git_refusal(_state(tmp_path, **kw))
        assert refusal is not None and phrase in refusal

    @pytest.mark.parametrize("kw", [{"dirty": True}, {"unpushed": True}])
    def test_the_repair_never_offers_allow_dirty(self, tmp_path, kw):
        # `--allow-dirty` is the NODE escape hatch (the node gets origin's
        # copy). The cloud clone is the same, but magent never offers it:
        # a cloud session that silently lacks the work is the failure here.
        refusal = nodes.cloud_git_refusal(_state(tmp_path, **kw))
        assert refusal is not None and "--allow-dirty" not in refusal

    def test_a_dirty_refusal_says_untracked_files_never_reach_the_session(
        self, tmp_path
    ):
        refusal = nodes.cloud_git_refusal(_state(tmp_path, dirty=True))
        assert refusal is not None and "untracked" in refusal

    def test_the_structural_refusals_are_the_node_checks_not_a_copy(self, tmp_path):
        # Composed over `refusal_for`, so the wording cannot drift from the
        # node path's.
        for kw in ({"url": ""}, {"detached": True}, {"no_commits": True}):
            state = _state(tmp_path, **kw)
            assert nodes.cloud_git_refusal(state) == nodes.refusal_for(
                state, allow_dirty=True
            )


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "api"
    repo.mkdir(exist_ok=True)
    (repo / ".env").write_text(
        "# comment\nexport API_TOKEN=hunter2-secret\nDB_URL = postgres://u:pw@h/db\n\nnot a line\n",
        encoding="utf-8",
    )
    (repo / ".env.local").write_text("EXTRA=1\n", encoding="utf-8")
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    return repo


_ALL = (".env", ".env.local", ".claude/settings.local.json")


class TestThePushSetForTheCloud:
    def test_dotenv_names_are_the_names_only(self, tmp_path):
        assert nodes.dotenv_names(_project(tmp_path) / ".env") == (
            "API_TOKEN",
            "DB_URL",
        )

    def test_indented_exported_and_crlf_lines_still_name_their_variable(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"  export  FOO = 1\r\nBAR=2\r\n\r\n\r\n  \r\n#BAZ=3\r\n")
        assert nodes.dotenv_names(path) == ("BAR", "FOO")

    def test_an_unreadable_env_file_names_nothing(self, tmp_path):
        assert nodes.dotenv_names(tmp_path / "missing.env") == ()

    def test_files_inside_the_project_travel_and_names_come_from_env_files(
        self, tmp_path
    ):
        repo = _project(tmp_path)
        ps = nodes.cloud_push_set(
            repo, [_state(repo, ignored=_ALL)], home=tmp_path / "home"
        )
        assert [ps.rel(p) for p in ps.files] == [
            ".claude/settings.local.json",
            ".env",
            ".env.local",
        ]
        assert [ps.rel(p) for p in ps.env_files] == [".env", ".env.local"]
        assert ps.names == ("API_TOKEN", "DB_URL", "EXTRA")
        assert ps.outside == ()

    def test_a_file_outside_the_project_is_named_but_never_travels(
        self, tmp_path, monkeypatch
    ):
        repo = _project(tmp_path)
        outside = tmp_path / "home" / ".npmrc"
        monkeypatch.setattr(nodes, "push_set", lambda *a, **k: [repo / ".env", outside])
        ps = nodes.cloud_push_set(repo, [_state(repo)], home=tmp_path / "home")
        assert ps.files == (repo / ".env",)
        assert ps.outside == (outside,)

    def test_the_existing_env_file_predicate_is_untouched(self):
        # J3 must not redefine nodes._is_env_file (it takes a NAME and
        # `_from_git_listing` depends on that).
        assert nodes._is_env_file(".env") and nodes._is_env_file(".env.local")
        assert not nodes._is_env_file("env.txt")


class TestTheDigestIsKeyedAndCoversValues:
    def _ps(self, tmp_path: Path) -> nodes.CloudPushSet:
        repo = _project(tmp_path)
        return nodes.cloud_push_set(
            repo, [_state(repo, ignored=(".env",))], home=tmp_path / "h"
        )

    def test_a_changed_value_changes_the_digest(self, tmp_path, cloud_home):
        # A rotated secret must be handed off again, so the digest covers bytes.
        ps = self._ps(tmp_path)
        before = nodes.push_set_digest(ps, "manual")
        (ps.project_dir / ".env").write_text(
            "API_TOKEN=rotated\nDB_URL=x\n", encoding="utf-8"
        )
        assert nodes.push_set_digest(ps, "manual") != before

    def test_the_mode_is_part_of_the_digest(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        assert nodes.push_set_digest(ps, "age1one") != nodes.push_set_digest(
            ps, "age1two"
        )

    def test_the_key_is_made_once_and_reused(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        assert nodes.push_set_digest(ps, "manual") == nodes.push_set_digest(
            ps, "manual"
        )
        assert len((cloud_home / "cloud-digest.key").read_bytes()) == 32

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_key_is_private_to_the_user(self, tmp_path, cloud_home):
        nodes.push_set_digest(self._ps(tmp_path), "manual")
        assert (cloud_home / "cloud-digest.key").stat().st_mode & 0o077 == 0


class TestTheCreateWaitsForThePushSet:
    def _ps(self, tmp_path: Path) -> nodes.CloudPushSet:
        repo = tmp_path / "api"
        repo.mkdir(exist_ok=True)
        (repo / ".env").write_text("API_TOKEN=hunter2-secret\n", encoding="utf-8")
        return nodes.cloud_push_set(
            repo, [_state(repo, ignored=(".env",))], home=tmp_path / "h"
        )

    def test_an_unhanded_push_set_refuses_and_names_the_repair(
        self, tmp_path, cloud_home
    ):
        refusal = nodes.cloud_env_refusal(
            "api", "api", self._ps(tmp_path), recipient=None
        )
        assert refusal is not None
        assert "API_TOKEN" in refusal
        assert "magent node push api" in refusal

    def test_a_sealed_push_set_lets_the_create_through(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "age1abc"), mode="age1abc"
        )
        assert nodes.cloud_env_refusal("api", "api", ps, recipient="age1abc") is None

    def test_a_rotated_key_asks_for_a_new_seal(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "age1old"), mode="age1old"
        )
        assert (
            nodes.cloud_env_refusal("api", "api", ps, recipient="age1new") is not None
        )

    def test_a_manual_hand_off_holds_whatever_the_key(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        assert nodes.cloud_env_refusal("api", "api", ps, recipient="age1any") is None

    def test_a_changed_value_asks_again(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        (ps.project_dir / ".env").write_text("API_TOKEN=rotated\n", encoding="utf-8")
        assert nodes.cloud_env_refusal("api", "api", ps, recipient=None) is not None

    def test_a_project_with_nothing_to_hand_off_is_never_refused(
        self, tmp_path, cloud_home
    ):
        repo = tmp_path / "bare"
        repo.mkdir()
        empty = nodes.cloud_push_set(repo, [_state(repo)], home=tmp_path / "h")
        assert nodes.cloud_env_refusal("bare", "bare", empty, recipient=None) is None

    def test_no_value_ever_reaches_the_refusal_the_record_or_the_repr(
        self, tmp_path, cloud_home
    ):
        ps = self._ps(tmp_path)
        refusal = nodes.cloud_env_refusal("api", "api", ps, recipient=None) or ""
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        record = (cloud_home / "cloud-records.json").read_text(encoding="utf-8")
        for text in (refusal, record, repr(ps)):
            assert "hunter2" not in text

    def test_a_torn_record_file_reads_as_no_record(self, tmp_path, cloud_home):
        cloud_home.mkdir(parents=True)
        (cloud_home / "cloud-records.json").write_text("{torn", encoding="utf-8")
        assert nodes.read_cloud_record("api") is None

    def test_a_deeply_nested_record_file_reads_as_no_record(self, tmp_path, cloud_home):
        # Every JSON file this module reads is refused past MAX_JSON_DEPTH
        # before json parses it, so a hostile or corrupt file cannot raise.
        cloud_home.mkdir(parents=True)
        (cloud_home / "cloud-records.json").write_text(
            "[" * 5000 + "]" * 5000, encoding="utf-8"
        )
        assert nodes.read_cloud_record("api") is None

    def test_a_record_for_one_project_leaves_the_others_alone(self, cloud_home):
        nodes.write_cloud_record("api", digest="d1", mode="manual")
        nodes.write_cloud_record("web", digest="d2", mode="age1abc", commit="c0ffee")
        assert nodes.read_cloud_record("api") == {
            "digest": "d1",
            "mode": "manual",
            "commit": "",
        }
        assert nodes.read_cloud_record("web") == {
            "digest": "d2",
            "mode": "age1abc",
            "commit": "c0ffee",
        }
        assert nodes.read_cloud_record("missing") is None

    def test_the_record_files_never_collide_with_the_reserved_names(self, cloud_home):
        # The cloud dir also holds `<sid>/` mirror dirs; a `cloud-<x>.<ext>`
        # file name can never be one, and none is a reserved per-node file.
        nodes.write_cloud_record("api", digest="d", mode="manual")
        nodes.write_recipient("age1qqqq")
        for path in cloud_home.iterdir():
            assert path.name.startswith("cloud-") and "." in path.name
            assert path.name not in nodes._RESERVED_NAMES

    def test_the_recipient_round_trips_and_rejects_anything_else(self, cloud_home):
        assert nodes.read_recipient() is None
        nodes.write_recipient("age1qqqq")
        assert nodes.read_recipient() == "age1qqqq"
        (cloud_home / "cloud-recipient.txt").write_text(
            "AGE-SECRET-KEY-1X", encoding="utf-8"
        )
        assert nodes.read_recipient() is None
