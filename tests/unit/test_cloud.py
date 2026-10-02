"""The cloud backend (sub-plan J, spec §18): a LOCAL psmux pane running
`claude --cloud`, created once, only from a clean pushed GitHub checkout, and
only after the push set was sealed or handed off."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import os
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING

import pytest

from magent import nodes
from magent.config import SCHEMA_VERSION, ConfigError, load_config
from magent.lockfile import LockHeld, persistent_lock
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
            # GitHub's ssh-over-443 host, the one a firewalled network needs.
            "ssh://git@ssh.github.com:443/me/api.git",
            "ssh://ssh.github.com/me/api.git",
            "git@ssh.github.com:me/api.git",
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
            # ssh.github.com serves ssh only, and only that exact host.
            "https://ssh.github.com/me/api.git",
            "https://ssh.github.com.evil.example/me/api.git",
            "ssh://git@evilssh.github.com/me/api.git",
            "git@evilssh.github.com:me/api.git",
            "git@xssh.github.com.evil.example:me/api.git",
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

    @pytest.mark.parametrize(
        "kw",
        [
            {"url": ""},
            {"detached": True},
            {"no_commits": True},
            {"url": "https://gitlab.com/me/api.git"},
            {"dirty": True},
            {"unpushed": True},
        ],
    )
    def test_every_refusal_names_the_checkout_first(self, tmp_path, kw):
        # A multi-project create prints several refusals; each must say whose.
        refusal = nodes.cloud_git_refusal(_state(tmp_path, **kw))
        assert refusal is not None and refusal.startswith(f"{tmp_path}: ")

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

    def test_a_utf8_bom_does_not_hide_the_first_name(self, tmp_path):
        # Notepad and PowerShell 5 write one; `^[ \t]*NAME` would skip line 1.
        path = tmp_path / ".env"
        path.write_bytes(b"\xef\xbb\xbfFIRST=1\r\nSECOND=2\r\n")
        assert nodes.dotenv_names(path) == ("FIRST", "SECOND")

    @pytest.mark.parametrize(
        ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    )
    def test_a_utf16_env_file_still_names_its_variables(self, tmp_path, bom, codec):
        # `echo X=1 > .env` in Windows PowerShell 5.1 writes UTF-16 LE + BOM.
        path = tmp_path / ".env"
        path.write_bytes(bom + "FIRST=1\r\nSECOND=2\r\n".encode(codec))
        assert nodes.dotenv_names(path) == ("FIRST", "SECOND")

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

    def test_a_project_reached_through_a_link_still_owns_its_files(self, tmp_path):
        # `_push` judges "inside" against the RESOLVED root, so a hit git lists
        # under the real path is inside a project configured as the link.
        real = _project(tmp_path)
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks here")
        ps = nodes.cloud_push_set(
            link, [_state(real, ignored=(".env",))], home=tmp_path / "h"
        )
        assert ps.outside == ()
        assert [ps.rel(p) for p in ps.files] == [".env"]
        assert ps.names == ("API_TOKEN", "DB_URL")

    def test_a_relative_project_dir_still_owns_its_files(self, tmp_path, monkeypatch):
        repo = _project(tmp_path)
        monkeypatch.chdir(tmp_path)
        ps = nodes.cloud_push_set(
            repo.relative_to(tmp_path),
            [_state(repo, ignored=(".env",))],
            home=tmp_path / "h",
        )
        assert ps.outside == ()
        assert [ps.rel(p) for p in ps.files] == [".env"]
        assert ps.names == ("API_TOKEN", "DB_URL")

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

    def test_a_different_key_gives_a_different_digest(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        first = nodes.push_set_digest(ps, "manual")
        (cloud_home / "cloud-digest.key").write_bytes(b"\x01" * 32)
        assert nodes.push_set_digest(ps, "manual") != first

    def test_the_digest_is_the_hmac_and_not_a_bare_hash(self, tmp_path, cloud_home):
        # A bare sha256 of a short secret is guessable offline; the key is what
        # stops a leaked record file from being a guessing oracle.
        ps = self._ps(tmp_path)
        digest = nodes.push_set_digest(ps, "manual")
        key = (cloud_home / "cloud-digest.key").read_bytes()
        message = nodes._digest_input(ps, "manual")
        assert digest == hmac.new(key, message, hashlib.sha256).hexdigest()
        assert digest != hashlib.sha256(message).hexdigest()

    def test_a_new_file_in_the_set_changes_the_digest(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        before = nodes.push_set_digest(ps, "manual")
        extra = ps.project_dir / ".env.local"
        extra.write_text("EXTRA=1\n", encoding="utf-8")
        bigger = dataclasses.replace(ps, files=(*ps.files, extra))
        assert nodes.push_set_digest(bigger, "manual") != before

    def test_a_changed_outside_path_changes_the_digest(self, tmp_path, cloud_home):
        ps = self._ps(tmp_path)
        npmrc = dataclasses.replace(ps, outside=(tmp_path / "h" / ".npmrc",))
        netrc = dataclasses.replace(ps, outside=(tmp_path / "h" / ".netrc",))
        digests = {nodes.push_set_digest(x, "manual") for x in (ps, npmrc, netrc)}
        assert len(digests) == 3

    def test_a_crafted_path_cannot_forge_a_field_boundary(self, tmp_path, cloud_home):
        # Unframed, outside=("a", "c") and outside=("a\no:c",) fed the same bytes.
        ps = self._ps(tmp_path)
        first, second = tmp_path / "a", tmp_path / "c"
        forged = type(tmp_path)(f"{first.as_posix()}\no:{second.as_posix()}")
        two = dataclasses.replace(ps, outside=(first, second))
        one = dataclasses.replace(ps, outside=(forged,))
        assert nodes.push_set_digest(one, "manual") != nodes.push_set_digest(
            two, "manual"
        )

    def test_the_fields_are_length_prefixed(self):
        assert nodes._framed(b"a", b"bc") != nodes._framed(b"ab", b"c")
        assert nodes._framed(b"", b"x") != nodes._framed(b"x", b"")

    def test_the_digest_does_not_depend_on_the_order_the_set_was_built_in(
        self, tmp_path, cloud_home
    ):
        # The function sorts, so a hand-built set cannot change what it says.
        repo = _project(tmp_path)
        built = nodes.cloud_push_set(
            repo, [_state(repo, ignored=_ALL)], home=tmp_path / "h"
        )
        ps = dataclasses.replace(
            built, outside=(tmp_path / "h" / ".netrc", tmp_path / "h" / ".npmrc")
        )
        assert len(ps.files) == 3
        flipped = dataclasses.replace(
            ps, files=ps.files[::-1], outside=ps.outside[::-1]
        )
        assert nodes.push_set_digest(flipped, "manual") == nodes.push_set_digest(
            ps, "manual"
        )

    def test_an_unreadable_file_is_signalled_not_hashed_as_a_token(
        self, tmp_path, cloud_home
    ):
        # `b"unreadable"` made a vanished file hash the same every time, so a
        # record taken while it was missing matched the next time it was.
        ps = self._ps(tmp_path)
        (ps.project_dir / ".env").unlink()
        with pytest.raises(nodes.PushSetUnreadable) as err:
            nodes.push_set_digest(ps, "manual")
        assert err.value.label == ".env"
        assert isinstance(err.value, OSError)
        assert ".env" in str(err.value)
        assert str(tmp_path) not in str(err.value)


class TestTheDigestKeyIsMadeOnceAndNeverRewritten:
    def _names(self, cloud_home: Path) -> list[str]:
        return sorted(p.name for p in cloud_home.iterdir())

    def test_an_intact_key_is_never_rewritten(self, cloud_home):
        cloud_home.mkdir(parents=True)
        path = cloud_home / "cloud-digest.key"
        path.write_bytes(b"K" * 32)
        before = path.stat()
        assert nodes._digest_key() == b"K" * 32
        after = path.stat()
        assert path.read_bytes() == b"K" * 32
        assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)
        assert self._names(cloud_home) == ["cloud-digest.key"]

    @pytest.mark.parametrize("junk", [b"", b"short", b"X" * 31, b"X" * 33, b"X" * 4096])
    def test_a_short_or_garbage_key_is_replaced_and_then_stable(self, cloud_home, junk):
        cloud_home.mkdir(parents=True)
        path = cloud_home / "cloud-digest.key"
        path.write_bytes(junk)
        key = nodes._digest_key()
        assert len(key) == 32
        assert path.read_bytes() == key
        assert nodes._digest_key() == key
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_first_creation_leaves_no_temp_file_behind(self, cloud_home):
        key = nodes._digest_key()
        assert (cloud_home / "cloud-digest.key").read_bytes() == key
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_a_lost_creation_race_returns_the_winners_key(
        self, cloud_home, monkeypatch
    ):
        # Another process links its key into place between our look and our
        # link: ours must not overwrite it, and we must hand back the winner's.
        winner = b"W" * 32

        def lose(src, dst):
            with open(dst, "wb") as fh:
                fh.write(winner)
            raise FileExistsError(dst)

        monkeypatch.setattr(os, "link", lose)
        assert nodes._digest_key() == winner
        assert (cloud_home / "cloud-digest.key").read_bytes() == winner
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_a_filesystem_without_hard_links_still_gets_a_key(
        self, cloud_home, monkeypatch
    ):
        def no_links(src, dst):
            raise PermissionError("hard links are not supported here")

        monkeypatch.setattr(os, "link", no_links)
        key = nodes._digest_key()
        assert len(key) == 32
        assert (cloud_home / "cloud-digest.key").read_bytes() == key
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_a_key_that_lands_while_links_fail_is_returned_not_overwritten(
        self, cloud_home, monkeypatch
    ):
        winner = b"W" * 32

        def plant_then_fail(src, dst):
            with open(dst, "wb") as fh:
                fh.write(winner)
            raise PermissionError("hard links are not supported here")

        monkeypatch.setattr(os, "link", plant_then_fail)
        assert nodes._digest_key() == winner
        assert (cloud_home / "cloud-digest.key").read_bytes() == winner
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_two_creators_without_hard_links_agree_on_one_key(
        self, cloud_home, monkeypatch
    ):
        # With no hard link the key can only be put in place by a rename, which
        # clobbers. Hold both creators in the gap between "no key yet" and the
        # rename: unserialized, both rename and each returns a key the other
        # destroyed; serialized, the second finds the first's key and returns it.
        def no_links(src, dst):
            raise PermissionError("hard links are not supported here")

        in_the_gap = threading.Barrier(2)
        real_replace = nodes._replace_retrying

        def gap_then_replace(src, dst):
            with contextlib.suppress(threading.BrokenBarrierError):
                in_the_gap.wait(timeout=0.5)
            real_replace(src, dst)

        monkeypatch.setattr(os, "link", no_links)
        monkeypatch.setattr(nodes, "_replace_retrying", gap_then_replace)
        keys: list[bytes] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                keys.append(nodes._digest_key())
            except Exception as exc:  # noqa: BLE001  # reason: collected and asserted empty below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(keys) == 2 and keys[0] == keys[1]
        assert (cloud_home / "cloud-digest.key").read_bytes() == keys[0]
        assert self._names(cloud_home) == ["cloud-digest.key"]

    def test_a_planted_symlink_is_replaced_never_written_through(
        self, tmp_path, cloud_home
    ):
        cloud_home.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"precious")
        path = cloud_home / "cloud-digest.key"
        try:
            path.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlinks here")
        key = nodes._digest_key()
        assert victim.read_bytes() == b"precious"
        assert not path.is_symlink()
        assert path.read_bytes() == key


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

    @pytest.mark.parametrize("body", ["", "# only a comment\n", "not a line\n"])
    def test_an_env_file_with_no_names_is_still_named_by_its_path(
        self, tmp_path, cloud_home, body
    ):
        repo = tmp_path / "api"
        repo.mkdir()
        (repo / ".env").write_text(body, encoding="utf-8")
        ps = nodes.cloud_push_set(
            repo, [_state(repo, ignored=(".env",))], home=tmp_path / "h"
        )
        assert ps.names == () and ps.outside == ()
        refusal = nodes.cloud_env_refusal("api", "api", ps, recipient=None)
        assert refusal is not None
        assert ".env" in refusal
        assert "0 file(s)" not in refusal
        assert "magent node push api" in refusal

    def test_only_files_outside_the_project_are_counted(
        self, tmp_path, cloud_home, monkeypatch
    ):
        repo = tmp_path / "api"
        repo.mkdir()
        monkeypatch.setattr(
            nodes, "push_set", lambda *a, **k: [tmp_path / "home" / ".npmrc"]
        )
        ps = nodes.cloud_push_set(repo, [_state(repo)], home=tmp_path / "home")
        assert ps.files == () and len(ps.outside) == 1
        refusal = nodes.cloud_env_refusal("api", "api", ps, recipient=None)
        assert refusal is not None and "1 file(s) outside the project" in refusal

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

    def test_the_digest_is_compared_in_constant_time_on_bytes(
        self, tmp_path, cloud_home, monkeypatch
    ):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        seen: list[tuple[object, object]] = []
        real = hmac.compare_digest

        def spy(a, b):
            seen.append((a, b))
            return real(a, b)

        monkeypatch.setattr(nodes.hmac, "compare_digest", spy)
        assert nodes.cloud_env_refusal("api", "api", ps, recipient=None) is None
        assert seen
        assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in seen)

    def test_a_non_ascii_digest_in_a_record_is_a_mismatch_not_a_crash(
        self, tmp_path, cloud_home
    ):
        # hmac.compare_digest raises TypeError on a non-ASCII str.
        ps = self._ps(tmp_path)
        nodes.write_cloud_record("api", digest="dé", mode="manual")
        assert nodes.cloud_env_refusal("api", "api", ps, recipient=None) is not None

    def test_an_unreadable_push_file_is_a_refusal_that_names_it(
        self, tmp_path, cloud_home
    ):
        ps = self._ps(tmp_path)
        nodes.write_cloud_record("api", digest="whatever", mode="manual")
        (ps.project_dir / ".env").unlink()
        refusal = nodes.cloud_env_refusal("api", "api", ps, recipient=None)
        assert refusal is not None
        assert ".env" in refusal
        assert "magent node push api" in refusal
        assert "hunter2" not in refusal
        assert str(tmp_path) not in refusal


class TestTheRecordsAreWhatTheyClaimToBe:
    def _put(self, cloud_home: Path, raw: str) -> None:
        cloud_home.mkdir(parents=True, exist_ok=True)
        (cloud_home / "cloud-records.json").write_text(raw, encoding="utf-8")

    @pytest.mark.parametrize(
        "raw",
        [
            '{"api": {}}',
            '{"api": {"mode": "manual"}}',
            '{"api": {"digest": "d"}}',
            '{"api": {"digest": null, "mode": "manual"}}',
            '{"api": {"digest": 7, "mode": "manual"}}',
            '{"api": {"digest": ["d"], "mode": "manual"}}',
            '{"api": {"digest": true, "mode": "manual"}}',
            '{"api": {"digest": "d", "mode": null}}',
            '{"api": {"digest": "d", "mode": "manual", "commit": 3}}',
            # A lone surrogate is valid JSON text but cannot be encoded: it
            # would crash the constant-time compare.
            '{"api": {"digest": "\\ud800", "mode": "manual"}}',
            '{"api": {"digest": "d", "mode": "\\udc00"}}',
            '{"api": {"digest": "d", "mode": "manual", "commit": "\\ud800"}}',
            '{"api": "not a record"}',
            '{"api": ["digest", "mode"]}',
        ],
    )
    def test_a_malformed_record_reads_as_no_record(self, cloud_home, raw):
        # Never a partial dict, and never `{}`: the docstring promises None.
        self._put(cloud_home, raw)
        assert nodes.read_cloud_record("api") is None

    def test_a_record_without_a_commit_reads_with_an_empty_one(self, cloud_home):
        self._put(cloud_home, '{"api": {"digest": "d", "mode": "manual"}}')
        assert nodes.read_cloud_record("api") == {
            "digest": "d",
            "mode": "manual",
            "commit": "",
        }

    def test_one_bad_record_does_not_take_the_good_ones_with_it(self, cloud_home):
        self._put(
            cloud_home,
            '{"bad": {"digest": 1}, "good": {"digest": "d", "mode": "manual"}}',
        )
        assert nodes.read_cloud_record("bad") is None
        assert nodes.read_cloud_record("good") is not None

    @pytest.mark.parametrize(
        "mode",
        ["", "other", "AGE-SECRET-KEY-1XYZ", "age1abc\nAGE-SECRET-KEY-1XYZ", "age1"],
    )
    def test_a_record_mode_is_manual_or_a_public_recipient(self, cloud_home, mode):
        # The recipient is stored in this plain-text file as the `mode`.
        with pytest.raises(ValueError, match="mode") as err:
            nodes.write_cloud_record("api", digest="d", mode=mode)
        assert "AGE-SECRET" not in str(err.value)
        assert nodes.read_cloud_record("api") is None

    def test_concurrent_writers_do_not_lose_each_others_records(
        self, cloud_home, monkeypatch
    ):
        real = nodes.write_json_atomic

        def slow(path, data):
            # Widen the read-modify-write window so a lost update is certain.
            time.sleep(0.02)
            real(path, data)

        monkeypatch.setattr(nodes, "write_json_atomic", slow)
        sids = [f"p{i}" for i in range(8)]
        errors: list[BaseException] = []

        def write(sid: str) -> None:
            try:
                nodes.write_cloud_record(sid, digest=f"d-{sid}", mode="manual")
            except BaseException as exc:  # noqa: BLE001  # reason: collected and asserted empty below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(s,)) for s in sids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        digests = {s: (nodes.read_cloud_record(s) or {}).get("digest") for s in sids}
        assert digests == {s: f"d-{s}" for s in sids}

    def test_a_write_that_cannot_get_the_lock_says_so(self, cloud_home):
        with (
            persistent_lock(nodes.CLOUD_RECORDS_LOCK_NAME, wait_s=1.0),
            pytest.raises(LockHeld),
        ):
            nodes.write_cloud_record("api", digest="d", mode="manual", wait_s=0.05)
        assert nodes.read_cloud_record("api") is None
        nodes.write_cloud_record("api", digest="d", mode="manual", wait_s=0.5)
        assert nodes.read_cloud_record("api") is not None


class TestTheRecipientIsOnePublicKey:
    def _put(self, cloud_home: Path, raw: str | bytes) -> None:
        cloud_home.mkdir(parents=True, exist_ok=True)
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        (cloud_home / "cloud-recipient.txt").write_bytes(data)

    @pytest.mark.parametrize(
        "text",
        [
            # The leak: it starts with age1, and a bare startswith returned
            # the identity verbatim into every record that stores the recipient.
            "age1abc\nAGE-SECRET-KEY-1XYZ",
            "age1abc\r\nAGE-SECRET-KEY-1XYZ\r\n",
            "age1abc AGE-SECRET-KEY-1XYZ",
            "age1abc\tAGE-SECRET-KEY-1XYZ",
            "AGE-SECRET-KEY-1XYZ",
            "age1",
            "age1ABC",
            "age1abc/def",
            "",
            "   \n",
        ],
    )
    def test_anything_but_one_public_key_reads_as_none(self, cloud_home, text):
        self._put(cloud_home, text)
        assert nodes.read_recipient() is None

    @pytest.mark.parametrize("raw", [b"\xff\xfe", b"age1abc\xff", b"\xff\xfeage1abc"])
    def test_a_file_that_is_not_utf8_reads_as_none(self, cloud_home, raw):
        self._put(cloud_home, raw)
        assert nodes.read_recipient() is None

    @pytest.mark.parametrize("text", ["age1abc", "age1abc\n", "  age1abc \r\n"])
    def test_one_public_key_reads_back_stripped(self, cloud_home, text):
        self._put(cloud_home, text)
        assert nodes.read_recipient() == "age1abc"

    @pytest.mark.parametrize(
        "bad",
        [
            "age1abc\nAGE-SECRET-KEY-1XYZ",
            "age1abc AGE-SECRET-KEY-1XYZ",
            "age1abc\n",
            "AGE-SECRET-KEY-1XYZ",
            "age1",
            "",
        ],
    )
    def test_writing_anything_but_one_public_key_is_refused(self, cloud_home, bad):
        with pytest.raises(ValueError, match="recipient") as err:
            nodes.write_recipient(bad)
        # The refusal never echoes what it refused.
        assert "AGE-SECRET" not in str(err.value)
        assert not (cloud_home / "cloud-recipient.txt").exists()

    def test_a_refused_write_leaves_the_old_recipient_alone(self, cloud_home):
        nodes.write_recipient("age1good")
        with pytest.raises(ValueError, match="recipient"):
            nodes.write_recipient("age1good\nAGE-SECRET-KEY-1XYZ")
        assert nodes.read_recipient() == "age1good"

    def test_one_predicate_judges_both_sides(self):
        assert nodes._is_recipient("age1qqqq")
        assert not nodes._is_recipient("age1qqqq\nAGE-SECRET-KEY-1X")
        assert not nodes._is_recipient("manual")


class TestOneDotenvDecoder:
    """``dotenv_names``, ``masked_lines`` and ``write_manual_handoff`` read a
    ``.env`` through ONE decoder, so they can never disagree about its text."""

    @pytest.mark.parametrize(
        ("raw", "text"),
        [
            (b"A=1\r\nB=2\n", "A=1\r\nB=2\n"),
            (b"\xef\xbb\xbfA=1\n", "A=1\n"),
            (b"\xff\xfe" + "A=1\r\n".encode("utf-16-le"), "A=1\r\n"),
            (b"\xfe\xff" + "A=1\r\n".encode("utf-16-be"), "A=1\r\n"),
            (b"", ""),
        ],
    )
    def test_a_byte_order_mark_picks_the_codec_and_never_reaches_the_text(
        self, tmp_path, raw, text
    ):
        path = tmp_path / ".env"
        path.write_bytes(raw)
        assert nodes._dotenv_text(path) == text

    def test_bytes_that_are_not_text_decode_lossily_instead_of_raising(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"A=\xff\xfe1\n")
        assert nodes._dotenv_text(path).startswith("A=")

    def test_an_unreadable_file_raises_the_os_error_for_the_caller_to_judge(
        self, tmp_path
    ):
        with pytest.raises(OSError):
            nodes._dotenv_text(tmp_path / "missing.env")


def _env_set(tmp_path: Path, files: dict[str, bytes]) -> nodes.CloudPushSet:
    """A push set whose ignored files are exactly ``files`` (name -> raw bytes)."""
    repo = tmp_path / "api"
    repo.mkdir(exist_ok=True)
    for name, raw in files.items():
        (repo / name).write_bytes(raw)
    return nodes.cloud_push_set(
        repo, [_state(repo, ignored=tuple(files))], home=tmp_path / "h"
    )


class TestTheManualHandOff:
    @pytest.fixture
    def private_tmp(self, tmp_path, monkeypatch) -> Path:
        """An empty directory standing in for the temp dir (``mkstemp`` reads
        ``tempfile.tempdir`` at call time): no test writes to the real one, and
        "no file left behind" is one ``iterdir`` away."""
        tmp = tmp_path / "tmp"
        tmp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(tmp))
        return tmp

    def _ps(self, tmp_path: Path) -> nodes.CloudPushSet:
        repo = tmp_path / "api"
        repo.mkdir()
        (repo / ".env").write_text(
            'API_TOKEN="hunter2-secret"\nDEBUG=1\n', encoding="utf-8"
        )
        return nodes.cloud_push_set(
            repo, [_state(repo, ignored=(".env",))], home=tmp_path / "h"
        )

    def test_the_terminal_lines_show_a_length_never_a_value(self, tmp_path):
        assert nodes.masked_lines(self._ps(tmp_path)) == [
            "API_TOKEN  ******** (14 chars)",
            "DEBUG  ******** (1 chars)",
        ]

    def test_the_value_appears_nowhere_in_the_terminal_lines(self, tmp_path):
        shown = "\n".join(nodes.masked_lines(self._ps(tmp_path)))
        assert "hunter2-secret" not in shown
        assert "hunter2" not in shown

    def test_quotes_blanks_and_a_trailing_space_are_measured_as_the_value(
        self, tmp_path
    ):
        ps = _env_set(tmp_path, {".env": b"A=\"xy\"\nB='z'\nC=\nexport  D = ddd  \n"})
        assert nodes.masked_lines(ps) == [
            "A  ******** (2 chars)",
            "B  ******** (1 chars)",
            "C  ******** (0 chars)",
            "D  ******** (3 chars)",
        ]

    def test_the_lines_name_the_variables_the_push_set_found(self, tmp_path):
        repo = _project(tmp_path)
        ps = nodes.cloud_push_set(
            repo, [_state(repo, ignored=_ALL)], home=tmp_path / "home"
        )
        # The same names `dotenv_names` found, spelled with the same grammar.
        assert [line.split("  ")[0] for line in nodes.masked_lines(ps)] == list(
            ps.names
        )

    @pytest.mark.parametrize(
        ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    )
    def test_a_utf16_env_file_is_measured_as_text(self, tmp_path, bom, codec):
        # Windows PowerShell 5.1's `>` writes UTF-16: not a NUL-riddled blank.
        ps = _env_set(
            tmp_path, {".env": bom + "FIRST=abc\r\nSECOND=1\r\n".encode(codec)}
        )
        assert nodes.masked_lines(ps) == [
            "FIRST  ******** (3 chars)",
            "SECOND  ******** (1 chars)",
        ]

    def test_a_utf8_bom_does_not_hide_the_first_name(self, tmp_path):
        ps = _env_set(tmp_path, {".env": b"\xef\xbb\xbfFIRST=1\r\nSECOND=2\r\n"})
        assert [line.split("  ")[0] for line in nodes.masked_lines(ps)] == [
            "FIRST",
            "SECOND",
        ]

    def test_an_unreadable_env_file_is_listed_not_skipped(self, tmp_path):
        # A hand-off that silently lacks a file would be pasted as if whole.
        ps = _env_set(tmp_path, {".env": b"DEBUG=1\n", ".env.local": b"X=1\n"})
        (tmp_path / "api" / ".env.local").unlink()
        lines = nodes.masked_lines(ps)
        assert lines == ["DEBUG  ******** (1 chars)", ".env.local  (could not be read)"]
        assert str(tmp_path) not in "\n".join(lines)

    def test_the_hand_off_file_is_the_env_text_in_a_private_temp_file(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        path = nodes.write_manual_handoff(self._ps(tmp_path))
        assert path.parent == tmp_path
        text = path.read_text(encoding="utf-8")
        assert 'API_TOKEN="hunter2-secret"' in text and "DEBUG=1" in text
        # Only these variables travel, and the user is told what does NOT.
        assert "untracked files never reach" in text
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600

    def test_each_env_file_is_introduced_by_its_project_relative_path(
        self, tmp_path, private_tmp
    ):
        ps = _env_set(tmp_path, {".env": b"A=1\n", ".env.local": b"B=2\n"})
        text = nodes.write_manual_handoff(ps).read_text(encoding="utf-8")
        assert "# from .env\nA=1\n# from .env.local\nB=2\n" in text
        assert str(tmp_path) not in text

    @pytest.mark.parametrize(
        ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    )
    def test_a_utf16_env_file_is_written_decoded_never_as_nul_riddled_bytes(
        self, tmp_path, private_tmp, bom, codec
    ):
        ps = _env_set(tmp_path, {".env": bom + "FIRST=1\r\nSECOND=é\r\n".encode(codec)})
        raw = nodes.write_manual_handoff(ps).read_bytes()
        assert b"\x00" not in raw
        lines = raw.decode("utf-8").splitlines()
        assert "FIRST=1" in lines and "SECOND=é" in lines

    def test_a_utf8_bom_never_lands_in_the_hand_off(self, tmp_path, private_tmp):
        # One BOM per FILE; the second file's would land mid-hand-off.
        ps = _env_set(
            tmp_path,
            {
                ".env": b"\xef\xbb\xbfFIRST=1\r\n",
                ".env.local": b"\xef\xbb\xbfSECOND=2\r\n",
            },
        )
        raw = nodes.write_manual_handoff(ps).read_bytes()
        assert b"\xef\xbb\xbf" not in raw
        lines = raw.decode("utf-8").splitlines()
        assert "FIRST=1" in lines and "SECOND=2" in lines

    def test_an_unreadable_env_file_refuses_the_hand_off_and_leaves_no_file(
        self, tmp_path, private_tmp
    ):
        ps = _env_set(
            tmp_path, {".env": b"TOKEN=hunter2-secret\n", ".env.local": b"X=1\n"}
        )
        (tmp_path / "api" / ".env.local").unlink()
        with pytest.raises(nodes.PushSetUnreadable) as err:
            nodes.write_manual_handoff(ps)
        # J3's conventions: the project-relative label, the error's CLASS.
        assert (err.value.label, err.value.reason) == (
            ".env.local",
            "FileNotFoundError",
        )
        assert isinstance(err.value, OSError)
        assert str(tmp_path) not in str(err.value)
        assert list(private_tmp.iterdir()) == []

    def test_crlf_env_files_land_with_lf_endings_only(self, tmp_path, private_tmp):
        ps = _env_set(tmp_path, {".env": b"A=1\r\nB=2\r\n"})
        raw = nodes.write_manual_handoff(ps).read_bytes()
        assert b"\r" not in raw
        assert b"# from .env\nA=1\nB=2\n" in raw

    def test_the_private_temp_holds_the_text_as_utf8_with_lf_endings(self, private_tmp):
        path = nodes.write_private_temp("a\nb é\n", prefix="t-", suffix=".txt")
        assert path.parent == private_tmp
        assert path.name.startswith("t-") and path.suffix == ".txt"
        assert path.read_bytes() == "a\nb é\n".encode()
        if sys.platform != "win32":
            assert path.stat().st_mode & 0o777 == 0o600

    def test_a_write_that_dies_part_way_leaves_no_secret_behind(
        self, private_tmp, monkeypatch
    ):
        real_fdopen = os.fdopen

        class _Dies:
            def __init__(self, fh) -> None:
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self._fh.close()

            def write(self, text: str) -> None:
                self._fh.write(text[:8])
                self._fh.flush()
                raise OSError("disk full")

        monkeypatch.setattr(
            os, "fdopen", lambda fd, *a, **k: _Dies(real_fdopen(fd, *a, **k))
        )
        with pytest.raises(OSError, match="disk full"):
            nodes.write_private_temp("API_TOKEN=hunter2-secret\n", prefix="t-")
        assert list(private_tmp.iterdir()) == []

    def test_text_that_cannot_be_encoded_leaves_no_file_behind(self, private_tmp):
        # Not an OSError: the cleanup covers any failure, not just a full disk.
        with pytest.raises(UnicodeEncodeError):
            nodes.write_private_temp("ok\ud800", prefix="t-")
        assert list(private_tmp.iterdir()) == []

    def test_a_file_object_that_cannot_be_made_leaves_no_file_and_no_open_handle(
        self, private_tmp, monkeypatch
    ):
        offered: list[int] = []

        def refuse(fd, *args, **kwargs):
            offered.append(fd)
            raise OSError("no handles")

        monkeypatch.setattr(os, "fdopen", refuse)
        with pytest.raises(OSError, match="no handles"):
            nodes.write_private_temp("x", prefix="t-")
        assert list(private_tmp.iterdir()) == []
        # The descriptor mkstemp opened was closed, not leaked: on every OS
        # (nothing in between opens a file, so the number is not reused).
        assert len(offered) == 1
        with pytest.raises(OSError):
            os.fstat(offered[0])


class TestTheEnvParserIsQuoteAware:
    """The names are PRINTED (``masked_lines``, ``cloud_env_refusal``), so a
    line inside a multi-line quoted value -- a PEM key's body -- must never come
    back as a variable name: it is a slice of a secret. ONE parser serves
    ``dotenv_names`` and ``masked_lines``, with one line model."""

    def _read(self, tmp_path: Path, raw: bytes) -> tuple[tuple[str, ...], list[str]]:
        ps = _env_set(tmp_path, {".env": raw})
        return nodes.dotenv_names(tmp_path / "api" / ".env"), nodes.masked_lines(ps)

    @pytest.mark.parametrize(
        ("line", "names"),
        [
            ("FOO=1", ["FOO"]),
            ("export FOO=1", ["FOO"]),
            ("  export  FOO = 1", ["FOO"]),
            ("\tFOO=1", ["FOO"]),
            ("export\tFOO=1", ["FOO"]),
            ("FOO =", ["FOO"]),
            ("export=1", ["export"]),
            ("#FOO=1", []),
            ("1FOO=1", []),
            ("FOO", []),
            ("export FOO", []),
            ("=1", []),
            (" ", []),
            # ONE variable: a second `NAME=` on the same line is part of its value.
            ("SECRET=abc FOO=bar", ["SECRET"]),
        ],
    )
    def test_a_variable_is_a_name_at_the_start_of_a_line_and_nothing_else(
        self, line, names
    ):
        assert [name for name, _ in nodes._dotenv_entries(line)] == names

    def test_a_pem_bodys_continuation_lines_are_never_names(self, tmp_path):
        raw = b'KEY="-----BEGIN\nAbCdEf1234Xyz==\n-----END"\nNEXT=1\n'
        names, lines = self._read(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        # 10 + newline + 15 + newline + 8: the whole value, counted once.
        assert lines == ["KEY  ******** (35 chars)", "NEXT  ******** (1 chars)"]
        shown = "\n".join(lines)
        assert not any(piece in shown for piece in ("AbCdEf", "Xyz", "BEGIN", "END"))

    def test_the_refusal_message_never_carries_a_slice_of_the_value(
        self, tmp_path, cloud_home
    ):
        ps = _env_set(
            tmp_path, {".env": b'KEY="-----BEGIN\nAbCdEf1234Xyz==\n-----END"\n'}
        )
        assert ps.names == ("KEY",)
        message = nodes.cloud_env_refusal("api", "api", ps, None)
        assert message is not None and "KEY" in message
        assert "AbCdEf" not in message and "Xyz" not in message

    def test_a_single_quoted_value_may_span_lines_too(self, tmp_path):
        raw = b"K='line one\nSecretTail=zzz\nend'\nA=1\n"
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "K")
        assert lines == ["A  ******** (1 chars)", "K  ******** (27 chars)"]

    def test_an_escaped_quote_does_not_close_a_double_quoted_value(self, tmp_path):
        # The `\"` before the line end is escaped, so `B=2` is still value.
        raw = b'A="say \\"hi\\"\nB=2\nend"\nC=1\n'
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "C")
        assert lines == ["A  ******** (18 chars)", "C  ******** (1 chars)"]

    def test_a_backslash_escapes_in_a_single_quoted_value_too(self, tmp_path):
        # python-dotenv and Node dotenv let a backslash escape the next
        # character inside '...'; bash does not. Where readers disagree the
        # parser takes the rule that LISTS FEWER names: under bash's rule
        # `A='first \'` closes at once and SLICE=zzz would print as a name.
        raw = b"A='first " + b"\\" + b"'\nSLICE=zzz\nend'\nNEXT=1\n"
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "NEXT")
        # first + backslash + quote (8) + newline + SLICE=zzz (9) + newline + end
        assert lines == ["A  ******** (22 chars)", "NEXT  ******** (1 chars)"]
        assert "SLICE" not in "\n".join(lines)

    def test_a_backtick_value_may_span_lines_too(self, tmp_path):
        # Node dotenv's multi-line form: `...`, with the same continuation.
        raw = b"K=`line one\nSecretTail=zzz\nend`\nA=1\n"
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "K")
        assert lines == ["A  ******** (1 chars)", "K  ******** (27 chars)"]

    def test_an_escaped_backtick_does_not_close_a_backtick_value(self, tmp_path):
        raw = b"K=`one " + b"\\" + b"`\nSLICE=zzz\nend`\nNEXT=1\n"
        names, _ = self._read(tmp_path, raw)
        assert names == ("K", "NEXT")

    def test_other_quote_characters_are_plain_text_inside_a_backtick_value(
        self, tmp_path
    ):
        # Only a backtick closes it: the `"` and `'` on the first line do not.
        raw = b'K=`say "hi" it\'s\nSLICE=zzz\nend`\nNEXT=1\n'
        names, _ = self._read(tmp_path, raw)
        assert names == ("K", "NEXT")

    def test_an_unterminated_backtick_swallows_the_rest_of_the_file(self, tmp_path):
        names, _ = self._read(tmp_path, b"A=`never closed\nX=1\nY=2\n")
        assert names == ("A",)

    def test_an_unterminated_quote_swallows_the_rest_of_the_file(self, tmp_path):
        # Under-listing is the safe direction: the tail of a secret is never
        # re-parsed as names.
        names, lines = self._read(tmp_path, b'A="never closed\nX=1\nY=2\n')
        assert names == ("A",)
        assert len(lines) == 1 and lines[0].startswith("A  ")

    def test_an_inline_comment_is_not_part_of_an_unquoted_value(self, tmp_path):
        raw = b"A=x # note\nB=y\t# tab\nC=#nospace\nD= # all comment\n"
        _, lines = self._read(tmp_path, raw)
        assert lines == [
            "A  ******** (1 chars)",
            "B  ******** (1 chars)",
            "C  ******** (8 chars)",
            "D  ******** (0 chars)",
        ]

    def test_a_hash_inside_quotes_is_part_of_the_value(self, tmp_path):
        _, lines = self._read(tmp_path, b'A="a#b"\nB=\'a # b\'\nC="x" # note\n')
        assert lines == [
            "A  ******** (3 chars)",
            "B  ******** (5 chars)",
            "C  ******** (1 chars)",
        ]

    @pytest.mark.parametrize(
        "sep", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\r"]
    )
    def test_only_a_newline_ends_a_line(self, tmp_path, sep):
        # `str.splitlines()` also splits on these; a .env reader does not, so
        # `FOO=bar` after one is part of SECRET's value, never a second name.
        raw = f"SECRET=abc{sep}FOO=bar\n".encode()
        names, lines = self._read(tmp_path, raw)
        assert names == ("SECRET",)
        assert lines == ["SECRET  ******** (11 chars)"]

    def test_crlf_files_count_no_carriage_returns(self, tmp_path):
        raw = b'A=1\r\nB="x\r\ny"\r\nC=3\r\n'
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "B", "C")
        assert lines == [
            "A  ******** (1 chars)",
            "B  ******** (3 chars)",
            "C  ******** (1 chars)",
        ]

    def test_export_lines_are_variables(self, tmp_path):
        raw = b'export A=1\n  export  B = "q"\n\texport\tC=3\n'
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "B", "C")
        assert lines == [
            "A  ******** (1 chars)",
            "B  ******** (1 chars)",
            "C  ******** (1 chars)",
        ]

    def test_an_empty_value_is_zero_chars_quoted_or_not(self, tmp_path):
        _, lines = self._read(tmp_path, b"A=\nB=\"\"\nC=''\nD=1\n")
        assert lines == [
            "A  ******** (0 chars)",
            "B  ******** (0 chars)",
            "C  ******** (0 chars)",
            "D  ******** (1 chars)",
        ]

    def test_what_follows_a_closing_quote_is_ignored_and_the_next_line_parses(
        self, tmp_path
    ):
        names, lines = self._read(tmp_path, b'A="x" junk\nB=1\n')
        assert names == ("A", "B")
        assert lines == ["A  ******** (1 chars)", "B  ******** (1 chars)"]

    def test_the_last_definition_of_a_name_wins(self, tmp_path):
        ps = _env_set(tmp_path, {".env": b"A=1\nA=22\n", ".env.local": b"A=333\nB=4\n"})
        assert nodes.masked_lines(ps) == [
            "A  ******** (3 chars)",
            "B  ******** (1 chars)",
        ]


class TestTheEnvParserCostIsLinear:
    """The parser reads user files, and ``masked_lines`` runs before anything
    is confirmed: one hostile or merely odd file must not stall it. Each shape
    is one an unanchored or backtracking pattern turns quadratic; the bound is
    generous (the linear parse takes milliseconds), so only a real blow-up
    trips it."""

    BOUND_S = 2.0

    def _timed(self, text: str) -> tuple[list[tuple[str, int]], float]:
        started = time.perf_counter()
        entries = nodes._dotenv_entries(text)
        return entries, time.perf_counter() - started

    def test_a_long_run_of_blanks_after_an_unquoted_value(self):
        # `[ \t]+#` retried from every blank of the run: quadratic without the
        # lookbehind that pins it to the start of the run.
        entries, took = self._timed("A=x" + " " * 400_000)
        assert entries == [("A", 1)]
        assert took < self.BOUND_S

    def test_a_long_run_of_blanks_that_ends_in_a_comment_still_cuts_it(self):
        entries, took = self._timed("A=x" + " " * 400_000 + "# note")
        assert entries == [("A", 1)]
        assert took < self.BOUND_S

    @pytest.mark.parametrize(
        "line",
        [
            " " * 400_000 + "!",
            "A" + " " * 400_000,
            "export" + " " * 400_000,
        ],
        ids=["blanks-then-junk", "name-then-blanks", "export-then-blanks"],
    )
    def test_a_long_run_of_blanks_in_a_line_that_is_not_a_variable(self, line):
        entries, took = self._timed(line)
        assert entries == []
        assert took < self.BOUND_S

    def test_a_million_character_quoted_value(self):
        entries, took = self._timed('K="' + "x" * 1_000_000 + '"\nNEXT=1\n')
        assert entries == [("K", 1_000_000), ("NEXT", 1)]
        assert took < self.BOUND_S

    def test_a_quoted_value_spanning_two_hundred_thousand_lines(self):
        body = "\n".join("line" for _ in range(200_000))
        entries, took = self._timed('K="' + body + '"\nNEXT=1\n')
        assert [name for name, _ in entries] == ["K", "NEXT"]
        assert took < self.BOUND_S

    def test_a_two_hundred_thousand_line_file(self):
        text = "".join(f"K{i}=v{i}\n" for i in range(200_000))
        entries, took = self._timed(text)
        assert len(entries) == 200_000
        assert took < self.BOUND_S


class TestAnUnreadableFileRefusalCarriesNoOsError:
    """``PushSetUnreadable`` names the file by label and the error by class,
    because the OS error's own text carries the absolute path. Chaining it
    (``from exc``) would put that path back into any traceback or Sentry
    capture of the refusal."""

    def _unreadable(self, tmp_path: Path) -> nodes.CloudPushSet:
        ps = _env_set(tmp_path, {".env": b"A=1\n"})
        (tmp_path / "api" / ".env").unlink()
        return ps

    def test_the_digest_site_hides_its_cause(self, tmp_path, cloud_home):
        ps = self._unreadable(tmp_path)
        with pytest.raises(nodes.PushSetUnreadable) as err:
            nodes.push_set_digest(ps, "manual")
        assert err.value.__cause__ is None
        assert err.value.__suppress_context__

    def test_the_hand_off_site_hides_its_cause(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        ps = self._unreadable(tmp_path)
        with pytest.raises(nodes.PushSetUnreadable) as err:
            nodes.write_manual_handoff(ps)
        assert err.value.__cause__ is None
        assert err.value.__suppress_context__
