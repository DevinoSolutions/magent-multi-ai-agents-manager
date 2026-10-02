"""The cloud backend (sub-plan J, spec §18): a LOCAL psmux pane running
`claude --cloud`, created once, only from a clean pushed GitHub checkout, and
only after the push set was sealed or handed off."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from magent.config import SCHEMA_VERSION, ConfigError, load_config

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

    @pytest.mark.parametrize("base", ["", r'"C:\Program Files\claude.exe"', "cl&aude"])
    def test_an_executable_it_cannot_type_safely_is_refused(self, base):
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError):
            cloud_pane_command(base, "t")

    def test_an_unsafe_task_is_refused_even_past_config_validation(self):
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError):
            cloud_pane_command("claude", 'x" & del *')
