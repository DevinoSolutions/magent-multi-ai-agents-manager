"""The cloud backend (sub-plan J, spec §18): a LOCAL psmux pane running
`claude --cloud`, created once, only from a clean pushed GitHub checkout, and
only after the push set was sealed or handed off."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from magent import cli, launch, node_auth, nodes, psmux
from magent.config import SCHEMA_VERSION, ConfigError, load_config
from magent.launch import RunOpts
from magent.lockfile import LockHeld, persistent_lock
from magent.nodes import LocalGitState
from magent.remote_mux import RemoteError

if TYPE_CHECKING:
    from pathlib import Path


def _cloud_cfg(
    tmp_config,
    tmp_path: Path,
    *,
    settings: dict[str, object] | None = None,
    **proj: object,
) -> str:
    project_dir = tmp_path / "api"
    project_dir.mkdir(exist_ok=True)
    entry: dict[str, object] = {
        "path": str(project_dir),
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    entry.update(proj)
    if entry["node"] is None:
        # `node=None` asks for the SAME project as an ordinary local one: the
        # keys are absent from the file (a JSON null is not a valid node).
        entry = {k: v for k, v in entry.items() if k not in ("node", "cloudTask")}
    config: dict[str, object] = {"version": SCHEMA_VERSION, "projects": [entry]}
    if settings is not None:
        config["settings"] = settings
    return tmp_config(config)


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


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """An empty directory standing in for the temp dir (``mkstemp`` and
    ``gettempdir`` read ``tempfile.tempdir`` at call time): no test writes to
    the real one, and "no file left behind" is one ``iterdir`` away."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


class TestTheManualHandOff:
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
        lines = nodes.masked_lines(ps)
        # The file's `not a line` is counted, never shown.
        assert lines[-1] == "(1 line(s) not shown)"
        # The same names `dotenv_names` found, spelled with the same grammar.
        assert [line.split("  ")[0] for line in lines[:-1]] == list(ps.names)

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


@pytest.fixture
def win_seam(monkeypatch) -> SimpleNamespace:
    """The ONE seam under the Windows branch: node_auth's create-with-a-private-
    DACL and its read-the-DACL-back, replaced with fakes that run on any OS. No
    test runs a real ACL call here (``test_the_real_file_...`` below is the one
    win32-only read-back, on a tmp file)."""
    seam = SimpleNamespace(
        events=[],
        attempts=[],
        sizes_at_check=[],
        private=True,
        create_error=None,
        collide=0,
    )

    def create(path) -> int:
        seam.events.append("create")
        seam.attempts.append(path)
        if seam.collide:
            seam.collide -= 1
            raise FileExistsError(17, "exists", str(path))
        if seam.create_error is not None:
            raise seam.create_error
        return os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )

    def verify(path) -> bool:
        seam.events.append("verify")
        seam.sizes_at_check.append(os.path.getsize(path))
        return seam.private

    monkeypatch.setattr(node_auth, "_win_create_private", create)
    monkeypatch.setattr(node_auth, "_win_dacl_is_private", verify)
    return seam


@pytest.fixture
def posix_seam(monkeypatch) -> SimpleNamespace:
    """``fchmod`` and the mode read-back, replaced so the POSIX branch's
    refusals can be driven on any OS: ``mode`` is what the file reads back as,
    ``chmod_error`` what the chmod raises."""
    seam = SimpleNamespace(calls=[], mode=0o600, chmod_error=None)
    real_fstat = os.fstat
    touched: set[int] = set()

    def fchmod(fd: int, mode: int) -> None:
        touched.add(fd)
        seam.calls.append(mode)
        if seam.chmod_error is not None:
            raise seam.chmod_error

    def fstat(fd: int):
        if fd in touched:
            return SimpleNamespace(st_mode=stat.S_IFREG | seam.mode)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fchmod", fchmod, raising=False)
    monkeypatch.setattr(os, "fstat", fstat)
    return seam


class TestTheHandOffFileIsPrivateBeforeAByteIsWritten:
    """``os.chmod(0o600)`` does nothing to a Windows ACL, and a temp file
    inherits %TEMP%'s, which on a real box grants other accounts Modify. So the
    secret is only ever written into a file that was proven private first, and
    when that cannot be had nothing is written at all."""

    def test_the_windows_file_is_made_private_at_creation_and_checked_empty(
        self, private_tmp, win_seam
    ):
        fd, path = node_auth._create_private_temp_win("t-", ".env")
        os.close(fd)
        assert path.parent == private_tmp
        assert path.name.startswith("t-") and path.suffix == ".env"
        assert win_seam.events == ["create", "verify"]
        assert win_seam.sizes_at_check == [0]

    def test_an_acl_that_reads_back_as_not_ours_is_a_refusal_and_the_file_goes(
        self, private_tmp, win_seam
    ):
        win_seam.private = False
        with pytest.raises(nodes.HandoffNotPrivate) as err:
            node_auth._create_private_temp_win("t-", ".env")
        assert err.value.reason == "not-private"
        assert isinstance(err.value, OSError)
        assert list(private_tmp.iterdir()) == []

    def test_a_create_that_fails_is_a_refusal_naming_the_class_and_no_path(
        self, private_tmp, win_seam
    ):
        win_seam.create_error = PermissionError(
            13, "Access is denied", str(private_tmp / "secret-place")
        )
        with pytest.raises(nodes.HandoffNotPrivate) as err:
            node_auth._create_private_temp_win("t-", ".env")
        assert err.value.reason == "PermissionError"
        assert "denied" not in str(err.value)
        assert "secret-place" not in str(err.value)
        assert err.value.__cause__ is None and err.value.__suppress_context__
        assert win_seam.events == ["create"]
        assert list(private_tmp.iterdir()) == []

    def test_a_taken_name_is_retried_under_a_new_one(self, private_tmp, win_seam):
        win_seam.collide = 2
        fd, _ = node_auth._create_private_temp_win("t-", ".env")
        os.close(fd)
        assert win_seam.events == ["create", "create", "create", "verify"]
        assert len({p.name for p in win_seam.attempts}) == 3

    def test_eight_taken_names_in_a_row_are_a_refusal(self, private_tmp, win_seam):
        win_seam.collide = 99
        with pytest.raises(nodes.HandoffNotPrivate) as err:
            node_auth._create_private_temp_win("t-", ".env")
        assert err.value.reason == "FileExistsError"
        assert win_seam.events == ["create"] * 8

    def test_nothing_is_written_when_no_private_file_can_be_had(
        self, private_tmp, win_seam, monkeypatch
    ):
        monkeypatch.setattr(nodes, "_open_private", node_auth._create_private_temp_win)
        win_seam.private = False
        monkeypatch.setattr(
            os,
            "fdopen",
            lambda *a, **k: pytest.fail("a secret was about to be written"),
        )
        with pytest.raises(nodes.HandoffNotPrivate):
            nodes.write_private_temp("API_TOKEN=hunter2-secret\n", prefix="t-")
        assert list(private_tmp.iterdir()) == []

    def test_the_secret_goes_in_only_after_the_check(
        self, private_tmp, win_seam, monkeypatch
    ):
        monkeypatch.setattr(nodes, "_open_private", node_auth._create_private_temp_win)
        path = nodes.write_private_temp("API_TOKEN=hunter2-secret\n", prefix="t-")
        assert path.read_bytes() == b"API_TOKEN=hunter2-secret\n"
        assert win_seam.events == ["create", "verify"]
        assert win_seam.sizes_at_check == [0]

    def test_this_platform_takes_its_own_branch(self, monkeypatch):
        taken: list[str] = []

        def fake(name: str):
            def _make(prefix, suffix):
                taken.append(name)
                return -1, prefix

            return _make

        monkeypatch.setattr(node_auth, "_create_private_temp_win", fake("win"))
        monkeypatch.setattr(node_auth, "_create_private_temp_posix", fake("posix"))
        node_auth.create_private_temp("t-", ".env")
        assert taken == ["win" if sys.platform == "win32" else "posix"]

    def test_the_hand_off_asks_node_auth_for_its_file(self, monkeypatch):
        asked: list[tuple[str, str]] = []

        def fake(prefix, suffix):
            asked.append((prefix, suffix))
            raise node_auth.PrivateFileRefused("not-private")

        monkeypatch.setattr(node_auth, "create_private_temp", fake)
        with pytest.raises(nodes.HandoffNotPrivate):
            nodes.write_private_temp("x", prefix="t-", suffix=".env")
        assert asked == [("t-", ".env")]

    def test_posix_chmods_to_0600_and_reads_the_mode_back_before_any_write(
        self, private_tmp, posix_seam
    ):
        fd, path = node_auth._create_private_temp_posix("t-", ".env")
        os.close(fd)
        assert posix_seam.calls == [0o600]
        assert path.parent == private_tmp and os.path.getsize(path) == 0

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o044, 0o666])
    def test_a_mode_that_does_not_stick_is_a_refusal_and_the_file_goes(
        self, private_tmp, posix_seam, mode
    ):
        posix_seam.mode = mode
        with pytest.raises(nodes.HandoffNotPrivate) as err:
            node_auth._create_private_temp_posix("t-", ".env")
        assert err.value.reason == "not-private"
        assert list(private_tmp.iterdir()) == []

    def test_a_chmod_that_fails_is_a_refusal_naming_the_class_and_no_path(
        self, private_tmp, posix_seam
    ):
        posix_seam.chmod_error = PermissionError(1, "denied", "/secret-place/x")
        with pytest.raises(nodes.HandoffNotPrivate) as err:
            node_auth._create_private_temp_posix("t-", ".env")
        assert err.value.reason == "PermissionError"
        assert "denied" not in str(err.value)
        assert "secret-place" not in str(err.value)
        assert err.value.__cause__ is None and err.value.__suppress_context__
        assert list(private_tmp.iterdir()) == []

    def test_an_interrupt_during_the_chmod_still_deletes_the_file(
        self, private_tmp, posix_seam
    ):
        posix_seam.chmod_error = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            node_auth._create_private_temp_posix("t-", ".env")
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.skipif(sys.platform != "win32", reason="DACLs are a Windows thing")
    def test_the_real_file_carries_this_users_dacl_alone(self, private_tmp):
        path = nodes.write_private_temp("API_TOKEN=hunter2-secret\n", prefix="t-")
        assert path.read_bytes() == b"API_TOKEN=hunter2-secret\n"
        assert node_auth._win_dacl_is_private(path)
        # The directory kept its own (inheriting) ACL: the file's is its own doing.
        assert node_auth._win_dacl(path) != node_auth._win_dacl(private_tmp)


def _age(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


class TestTheSweepClearsOnlyOurOwnLeftovers:
    HOUR = 3600

    def _leftover(self, tmp, name="magent-cloud-env-aaaa.env", age=2 * HOUR):
        path = tmp / name
        path.write_text("API_TOKEN=hunter2-secret\n", encoding="utf-8")
        _age(path, age)
        return path

    def test_it_removes_old_hand_off_files_and_counts_them(self, private_tmp):
        self._leftover(private_tmp, "magent-cloud-env-aaaa.env")
        self._leftover(private_tmp, "magent-cloud-env-bbbb.env")
        assert nodes.sweep_handoff_leftovers() == (2, 0)
        assert list(private_tmp.iterdir()) == []

    def test_a_recent_file_is_left_alone(self, private_tmp):
        # A push in another terminal may be waiting at its prompt, or the user
        # may be pasting from a --yes file right now.
        kept = self._leftover(private_tmp, age=60)
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert kept.exists()

    def test_the_default_age_is_an_hour(self, private_tmp):
        young = self._leftover(private_tmp, "magent-cloud-env-young.env", age=59 * 60)
        old = self._leftover(private_tmp, "magent-cloud-env-old.env", age=61 * 60)
        assert nodes.sweep_handoff_leftovers() == (1, 0)
        assert young.exists() and not old.exists()

    def test_only_our_prefix_and_suffix_are_touched(self, private_tmp):
        others = [
            self._leftover(private_tmp, name)
            for name in (
                "notes.env",
                "magent-cloud-env-aaaa.txt",
                "other-magent-cloud-env-aaaa.env",
                "magent-cloud-env.env.bak",
            )
        ]
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert all(p.exists() for p in others)

    def test_a_directory_with_our_name_is_not_touched(self, private_tmp):
        folder = private_tmp / "magent-cloud-env-dir.env"
        folder.mkdir()
        (folder / "inside.txt").write_text("keep", encoding="utf-8")
        _age(folder, 2 * self.HOUR)
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert (folder / "inside.txt").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege")
    def test_a_link_is_neither_followed_nor_removed(self, private_tmp, tmp_path):
        target = tmp_path / "precious.env"
        target.write_text("keep", encoding="utf-8")
        _age(target, 2 * self.HOUR)
        link = private_tmp / "magent-cloud-env-link.env"
        link.symlink_to(target)
        then = time.time() - 2 * self.HOUR
        os.utime(link, (then, then), follow_symlinks=False)
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert link.is_symlink() and target.exists()

    def test_a_path_that_lstat_calls_a_link_is_left_alone_though_its_target_is_old(
        self, private_tmp, monkeypatch
    ):
        # Platform-neutral: a Windows box without the symlink privilege runs it
        # too. The path is a plain old file when FOLLOWED, a link when asked
        # about itself -- and only the second is what the sweep may go by.
        linked = self._leftover(private_tmp)
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(linked):
                return SimpleNamespace(
                    st_mode=stat.S_IFLNK | 0o777,
                    st_mtime=real_lstat(path).st_mtime,
                    st_uid=0,
                )
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert linked.exists()

    def test_another_users_file_is_left_alone(self, private_tmp, monkeypatch):
        # Platform-neutral too: on a shared /tmp a file of ours-by-name can
        # belong to another account.
        theirs = self._leftover(private_tmp)
        other = os.stat(theirs).st_uid + 1
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "getuid", lambda: other, raising=False)
        assert nodes.sweep_handoff_leftovers() == (0, 0)
        assert theirs.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
    def test_a_file_of_the_current_user_is_cleared_with_the_real_uid(self, private_tmp):
        self._leftover(private_tmp)
        assert nodes.sweep_handoff_leftovers() == (1, 0)

    def test_no_file_is_ever_read(self, private_tmp, monkeypatch):
        import builtins

        self._leftover(private_tmp)

        def no_open(*args, **kwargs):
            pytest.fail("the sweep opened a file")

        monkeypatch.setattr(builtins, "open", no_open)
        monkeypatch.setattr(type(private_tmp), "open", no_open)
        assert nodes.sweep_handoff_leftovers() == (1, 0)

    def test_a_file_that_will_not_go_is_counted_not_hidden(
        self, private_tmp, monkeypatch
    ):
        stuck = self._leftover(private_tmp)

        def refuse(self, missing_ok=False):
            raise PermissionError(13, "in use", str(self))

        monkeypatch.setattr(type(private_tmp), "unlink", refuse)
        assert nodes.sweep_handoff_leftovers() == (0, 1)
        assert stuck.exists()

    def test_a_file_that_vanished_first_is_not_counted(self, private_tmp, monkeypatch):
        self._leftover(private_tmp)

        def gone(self, missing_ok=False):
            raise FileNotFoundError(2, "gone", str(self))

        monkeypatch.setattr(type(private_tmp), "unlink", gone)
        assert nodes.sweep_handoff_leftovers() == (0, 0)

    def test_what_the_hand_off_writes_is_what_the_sweep_clears(
        self, tmp_path, private_tmp
    ):
        path = nodes.write_manual_handoff(_env_set(tmp_path, {".env": b"A=1\n"}))
        _age(path, 2 * self.HOUR)
        assert nodes.sweep_handoff_leftovers() == (1, 0)
        assert not path.exists()


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

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_a_quote_right_after_a_backslash_never_closes_the_value(
        self, tmp_path, quote
    ):
        # `A="x\\"`: the regex readers (python-dotenv, Node dotenv) see `\"` as
        # an escaped quote and keep reading; pairing the two backslashes and
        # closing would print SLICE=zzz, a slice of the value, as a name.
        raw = f"A={quote}x\\\\{quote}\nSLICE=zzz\ny{quote}\nNEXT=1\n".encode()
        names, lines = self._read(tmp_path, raw)
        assert names == ("A", "NEXT")
        # x + 2 backslashes + quote (4) + newline + SLICE=zzz (9) + newline + y
        assert lines == ["A  ******** (16 chars)", "NEXT  ******** (1 chars)"]

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_a_value_ending_in_a_backslash_swallows_the_rest_of_the_file(
        self, tmp_path, quote
    ):
        # The accepted cost of the rule above: a Windows path `C:\logs\` ends
        # in a backslash, so its closing quote reads as escaped and the rest of
        # the file is never listed. Under-listing is the safe direction.
        raw = f"LOG={quote}C:\\logs\\{quote}\nNEXT=1\nMORE=2\n".encode()
        names, lines = self._read(tmp_path, raw)
        assert names == ("LOG",)
        assert len(lines) == 2 and lines[0].startswith("LOG  ")
        assert lines[1] == "(2 line(s) not shown)"

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
        assert len(lines) == 2 and lines[0].startswith("A  ")
        # What it swallowed is counted, never listed.
        assert lines[1] == "(2 line(s) not shown)"

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


def _read_env(tmp_path: Path, raw: bytes) -> tuple[tuple[str, ...], list[str]]:
    """``dotenv_names`` and ``masked_lines`` over one env file's raw bytes."""
    ps = _env_set(tmp_path, {".env": raw})
    return nodes.dotenv_names(tmp_path / "api" / ".env"), nodes.masked_lines(ps)


# What a continuation line of a secret would print AS a name if it were parsed
# as a variable: `FRAG=` (an empty value) or `FRAG2==` (a base64 body's padding).
FRAG = "SENTINELfragment"


def _no_fragment(names: tuple[str, ...], lines: list[str]) -> None:
    shown = "\n".join([*names, *lines])
    assert "SENTINEL" not in shown and FRAG not in shown


class TestAMultiLineSecretNeverPrintsAName:
    """The names are PRINTED, so a line that is part of a secret must never
    come back as one. ``TestTheEnvParserIsQuoteAware`` pins the quoted forms;
    these are the shapes a quote does not cover: an unquoted PEM, a value the
    shell joins from adjacent quoted runs, and a "name" nobody would pick. The
    hand-off FILE still carries every byte -- only the screen is guarded."""

    # ---- unquoted armor ----------------------------------------------------

    def test_an_unquoted_pem_is_one_value_to_its_matching_end(self, tmp_path):
        block = [
            "-----BEGIN PRIVATE KEY-----",
            f"{FRAG}=",
            "MIIEvQIBADANBg",
            f"{FRAG}2==",
            "-----END PRIVATE KEY-----",
        ]
        raw = ("KEY=" + "\n".join(block) + "\nNEXT=1\n").encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        assert lines == [
            f"KEY  ******** ({len(chr(10).join(block))} chars)",
            "NEXT  ******** (1 chars)",
        ]
        _no_fragment(names, lines)

    def test_armor_on_its_own_lines_is_never_parsed_as_variables(self, tmp_path):
        raw = (
            f"KEY=\n-----BEGIN CERTIFICATE-----\n{FRAG}=\n{FRAG}2==\n"
            "-----END CERTIFICATE-----\nNEXT=1\n"
        ).encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        # BEGIN, two body lines and END: counted, never shown.
        assert lines == [
            "KEY  ******** (0 chars)",
            "NEXT  ******** (1 chars)",
            "(4 line(s) not shown)",
        ]
        _no_fragment(names, lines)

    def test_a_bundle_of_armored_blocks_is_swallowed_whole(self, tmp_path):
        raw = (
            f"CHAIN=-----BEGIN CERTIFICATE-----\n{FRAG}=\n-----END CERTIFICATE-----\n"
            f"-----BEGIN CERTIFICATE-----\n{FRAG}2=\n-----END CERTIFICATE-----\n"
            "NEXT=1\n"
        ).encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("CHAIN", "NEXT")
        _no_fragment(names, lines)

    def test_only_the_matching_end_closes_a_block(self, tmp_path):
        raw = (
            f"KEY=-----BEGIN A-----\n{FRAG}=\n-----END B-----\n{FRAG}2=\n"
            "-----END A-----\nNEXT=1\n"
        ).encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        _no_fragment(names, lines)

    def test_an_opening_line_that_names_no_label_ends_at_any_end(self, tmp_path):
        raw = f"KEY=-----BEGIN\n{FRAG}=\n-----END X-----\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        _no_fragment(names, lines)

    def test_an_unterminated_block_swallows_the_rest_of_the_file(self, tmp_path):
        raw = f"KEY=-----BEGIN PRIVATE KEY-----\n{FRAG}=\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY",)
        assert lines[1:] == ["(2 line(s) not shown)"]
        _no_fragment(names, lines)

    def test_a_block_that_opens_and_closes_on_one_line_ends_there(self, tmp_path):
        value = "-----BEGIN X-----abc-----END X-----"
        names, lines = _read_env(tmp_path, f"KEY={value}\nNEXT=1\n".encode())
        assert names == ("KEY", "NEXT")
        assert lines[0] == f"KEY  ******** ({len(value)} chars)"

    @pytest.mark.parametrize(
        "block",
        [
            "-----BEGIN X-----abc-----END X-----",
            "-----BEGIN X-----\nabc\n-----END X-----",
        ],
        ids=["one-line", "three-lines"],
    )
    def test_blanks_after_the_closing_line_are_not_part_of_the_value(
        self, tmp_path, block
    ):
        _, lines = _read_env(tmp_path, f"KEY={block}  \t\nNEXT=1\n".encode())
        assert lines[0] == f"KEY  ******** ({len(block)} chars)"

    def test_export_and_leading_blanks_do_not_hide_an_opening(self, tmp_path):
        raw = f"export KEY=  -----BEGIN K-----\n{FRAG}=\n-----END K-----\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        _no_fragment(names, lines)

    def test_dashes_that_are_not_an_opening_are_ordinary_text(self, tmp_path):
        # Neither inside a value nor at a line start does this open anything.
        raw = b"A=x-----BEGIN\nB=1\n----- notes\nC=2\n"
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "B", "C")
        assert lines[-1] == "(1 line(s) not shown)"

    @pytest.mark.parametrize(
        ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    )
    def test_armor_in_a_utf16_file_is_one_value(self, tmp_path, bom, codec):
        text = f"KEY=-----BEGIN K-----\r\n{FRAG}=\r\n-----END K-----\r\nNEXT=1\r\n"
        names, lines = _read_env(tmp_path, bom + text.encode(codec))
        assert names == ("KEY", "NEXT")
        _no_fragment(names, lines)

    def test_armor_in_a_crlf_file_is_one_value(self, tmp_path):
        raw = f"KEY=-----BEGIN K-----\r\n{FRAG}=\r\n-----END K-----\r\nNEXT=1\r\n"
        names, lines = _read_env(tmp_path, raw.encode())
        assert names == ("KEY", "NEXT")
        _no_fragment(names, lines)

    # ---- adjacent quoted runs ----------------------------------------------

    @pytest.mark.parametrize(
        "head",
        [
            "A='x''",
            'A="x""',
            "A='x'\"",
            'A="x"\'',
            "A='x''y'\"",
            'A="x"y\'',
            "export A='x''",
            "A='a # b''",
        ],
        ids=[
            "single-single",
            "double-double",
            "single-double",
            "double-single",
            "three-runs",
            "unquoted-between",
            "export",
            "hash-inside",
        ],
    )
    def test_adjacent_quoted_runs_are_one_value(self, tmp_path, head):
        # The shell joins them: the second run opens where the first closed and
        # keeps going over the next lines, so FRAG= below is part of A.
        quote = head[-1]
        raw = f"{head}\n{FRAG}=1\n{quote}\nB=2\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "B")
        _no_fragment(names, lines)

    def test_adjacent_runs_in_a_crlf_file_count_no_carriage_returns(self, tmp_path):
        raw = f"A='x''\r\n{FRAG}=1\r\n'\r\nB=2\r\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "B")
        # x, newline, FRAG=1, newline: both runs counted, the CRs never.
        assert lines[0] == f"A  ******** ({1 + 1 + len(FRAG) + 2 + 1} chars)"
        _no_fragment(names, lines)

    @pytest.mark.parametrize(
        ("line", "length"),
        [("A='a''b'", 2), ("A='a'\"b\"", 2), ("A=\"a\"'b''c'", 3), ("A='a'b", 1)],
    )
    def test_adjacent_runs_measure_the_joined_value(self, tmp_path, line, length):
        _, lines = _read_env(tmp_path, f"{line}\n".encode())
        assert lines == [f"A  ******** ({length} chars)"]

    def test_a_blank_ends_the_value_so_a_comment_may_hold_a_quote(self, tmp_path):
        # `# it's` after a blank is a comment, not an opening: the pin that keeps
        # the word rule from swallowing the file on every apostrophe.
        names, lines = _read_env(tmp_path, b'C="x" # it\'s a note\nB=1\n')
        assert names == ("B", "C")
        assert lines == ["B  ******** (1 chars)", "C  ******** (1 chars)"]

    # ---- what is allowed to be a name --------------------------------------

    def test_a_name_at_the_cap_is_printed_and_one_past_it_is_not(self, tmp_path):
        cap = nodes._DOTENV_NAME_MAX
        fits, over = "A" * cap, "B" * (cap + 1)
        names, lines = _read_env(tmp_path, f"{fits}=1\n{over}=2\nOK=3\n".encode())
        assert names == (fits, "OK")
        assert lines == [
            f"{fits}  ******** (1 chars)",
            "OK  ******** (1 chars)",
            "(1 line(s) not shown)",
        ]
        assert over not in "\n".join([*names, *lines])

    def test_a_base64_body_line_is_not_a_name_however_long(self, tmp_path):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 125
        names, lines = _read_env(tmp_path, f"{body}=\nOK=1\n".encode())
        assert names == ("OK",)
        assert body[:40] not in "\n".join(lines)

    @pytest.mark.parametrize(
        "line",
        ["my-key=1", "a.b=1", "é=1", "1A=1", "A B=1", "export FOO", "KEY:1"],
    )
    def test_only_an_identifier_is_ever_a_name(self, tmp_path, line):
        names, lines = _read_env(tmp_path, f"{line}\nOK=1\n".encode())
        assert names == ("OK",)
        assert lines == ["OK  ******** (1 chars)", "(1 line(s) not shown)"]

    @pytest.mark.parametrize("line", ["abc123==", "A==b", "A = =x"])
    def test_a_value_that_starts_with_an_equals_sign_is_base64_padding(
        self, tmp_path, line
    ):
        # `abc123==` is what the last line of a base64 body looks like. A real
        # variable never starts its value with `=`; when in doubt, no name.
        names, lines = _read_env(tmp_path, f"{line}\nOK=1\n".encode())
        assert names == ("OK",)
        assert lines == ["OK  ******** (1 chars)", "(1 line(s) not shown)"]

    def test_an_equals_sign_later_in_a_value_is_ordinary(self, tmp_path):
        names, _ = _read_env(tmp_path, b"URL=https://x.test/?a=b\nPAD=abc=\nE=\n")
        assert names == ("E", "PAD", "URL")

    # ---- the count line ----------------------------------------------------

    def test_the_count_sums_every_env_file_and_carries_no_text(self, tmp_path):
        ps = _env_set(
            tmp_path,
            {
                ".env": f"{FRAG} one\nA=1\n".encode(),
                ".env.local": (
                    f"-----BEGIN X-----\n{FRAG}=\n-----END X-----\nB=2\n"
                ).encode(),
            },
        )
        lines = nodes.masked_lines(ps)
        assert lines == [
            "A  ******** (1 chars)",
            "B  ******** (1 chars)",
            "(4 line(s) not shown)",
        ]
        _no_fragment((), lines)

    def test_comments_and_blank_lines_are_not_counted(self, tmp_path):
        _, lines = _read_env(tmp_path, b"# a comment\n\n   \n\t# indented\nA=1\n")
        assert lines == ["A  ******** (1 chars)"]

    def test_a_withheld_names_quoted_value_is_still_consumed(self, tmp_path):
        # The over-cap line is not shown, but its continuation lines are still
        # its value: they must not surface as names either.
        over = "B" * (nodes._DOTENV_NAME_MAX + 1)
        raw = f'{over}="one\n{FRAG}=\ntwo"\nOK=1\n'.encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("OK",)
        _no_fragment(names, lines)
        assert lines[-1] == "(1 line(s) not shown)"


class TestWhatFollowsAnUnaccountedLine:
    """A line the parser cannot account for (not blank, not a comment, not an
    assignment) may be a slice of a secret, and the lines after it may be the
    rest of the slice. ``TestAMultiLineSecretNeverPrintsAName`` pins the shapes
    that carry a marker (a quote, armor); these carry none: raw base64 whose
    last line ends in ``=`` reads as ``NAME=`` with an empty value. So an
    unquoted EMPTY value straight after a withheld line is withheld too --
    counted, never shown -- and a blank or comment line ends the doubt. The
    hand-off FILE and the seal never go through any of this."""

    # Two lines of raw base64, as the body of an unquoted key prints itself.
    B64 = ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "BKcwggSjAgEAAoIBAQC7/VJTUt9Us8cKj")

    # ---- the context rule ----------------------------------------------------

    @pytest.mark.parametrize("last", [f"{FRAG}=", "AAA="], ids=["fragment", "padding"])
    def test_raw_base64_over_three_lines_prints_no_name(self, tmp_path, last):
        raw = f"{self.B64[0]}\n{self.B64[1]}\n{last}\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(3 line(s) not shown)"]
        _no_fragment(names, lines)

    def test_a_body_on_its_own_lines_after_an_empty_key_prints_no_name(self, tmp_path):
        raw = f"KEY=\n{self.B64[0]}\n{FRAG}=\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        assert lines == [
            "KEY  ******** (0 chars)",
            "NEXT  ******** (1 chars)",
            "(2 line(s) not shown)",
        ]
        _no_fragment(names, lines)

    def test_a_body_that_starts_on_the_key_line_prints_no_name(self, tmp_path):
        raw = f"KEY={self.B64[0]}\n{self.B64[1]}\n{FRAG}=\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("KEY", "NEXT")
        assert lines == [
            f"KEY  ******** ({len(self.B64[0])} chars)",
            "NEXT  ******** (1 chars)",
            "(2 line(s) not shown)",
        ]
        _no_fragment(names, lines)

    def test_a_bare_word_then_an_empty_assignment_prints_no_name(self, tmp_path):
        names, lines = _read_env(tmp_path, b"export FOO\nBAR=\nNEXT=1\n")
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(2 line(s) not shown)"]
        assert "BAR" not in "\n".join(lines)

    def test_the_chain_runs_through_every_withheld_entry(self, tmp_path):
        raw = b"export FOO\nBAR=\nBAZ=\nQUX = # c\nNEXT=1\n"
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(4 line(s) not shown)"]

    @pytest.mark.parametrize(
        "empty",
        ["BAR=", "BAR =", "BAR=   ", "BAR= # note", "export BAR=", "\tBAR=\t"],
    )
    def test_every_spelling_of_an_empty_value_is_withheld(self, tmp_path, empty):
        names, lines = _read_env(tmp_path, f"export FOO\n{empty}\nNEXT=1\n".encode())
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(2 line(s) not shown)"]

    @pytest.mark.parametrize(
        "sep",
        ["", "   ", "\t", "# a note", "  # indented"],
        ids=["blank", "spaces", "tab", "comment", "indented-comment"],
    )
    def test_a_blank_or_comment_line_ends_the_doubt(self, tmp_path, sep):
        # `DEBUG=` and friends are real settings, and they stay listed.
        raw = f"export FOO\n{sep}\nDEBUG=\nverbose=\nhttp_proxy=\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("DEBUG", "http_proxy", "verbose")
        assert lines == [
            "DEBUG  ******** (0 chars)",
            "http_proxy  ******** (0 chars)",
            "verbose  ******** (0 chars)",
            "(1 line(s) not shown)",
        ]

    def test_a_value_that_says_something_is_shown_after_junk(self, tmp_path):
        names, lines = _read_env(tmp_path, b"export FOO\nBAR=1\nBAZ=x y\n")
        assert names == ("BAR", "BAZ")
        assert lines == [
            "BAR  ******** (1 chars)",
            "BAZ  ******** (3 chars)",
            "(1 line(s) not shown)",
        ]

    @pytest.mark.parametrize("empty", ['""', "''", "``"])
    def test_an_empty_quoted_value_is_a_statement_not_an_artifact(
        self, tmp_path, empty
    ):
        names, lines = _read_env(tmp_path, f"export FOO\nBAR={empty}\n".encode())
        assert names == ("BAR",)
        assert lines == ["BAR  ******** (0 chars)", "(1 line(s) not shown)"]

    def test_a_shown_entry_ends_the_doubt(self, tmp_path):
        names, lines = _read_env(tmp_path, b"export FOO\nX=1\nY=\nZ=\n")
        assert names == ("X", "Y", "Z")
        assert lines[-1] == "(1 line(s) not shown)"

    @pytest.mark.parametrize(
        ("withheld", "counted"),
        [
            ("-----BEGIN X-----\nabc\n-----END X-----", 3),
            ("B" * (nodes._DOTENV_NAME_MAX + 1) + "=1", 1),
            ("abc123==", 1),
        ],
        ids=["armor-block", "over-long-name", "padding"],
    )
    def test_every_kind_of_withheld_line_hands_the_doubt_on(
        self, tmp_path, withheld, counted
    ):
        raw = f"{withheld}\nBAR=\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        assert lines == [
            "NEXT  ******** (1 chars)",
            f"({counted + 1} line(s) not shown)",
        ]

    # ---- armor anywhere in a line that is not an assignment -----------------

    @pytest.mark.parametrize(
        "opener",
        [
            '"k": "-----BEGIN X-----',
            "cert: -----BEGIN X-----",
            '  ["-----BEGIN X-----',
            "}-----BEGIN X-----",
        ],
        ids=["json-key", "colon", "bracket", "brace"],
    )
    def test_a_begin_marker_anywhere_in_a_line_opens_a_block(self, tmp_path, opener):
        # `FRAG=1` is not empty, so the context rule would not hide it: only the
        # block does.
        raw = f'{opener}\n{FRAG}=1\n-----END X-----",\nNEXT=1\n'.encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(3 line(s) not shown)"]
        _no_fragment(names, lines)

    def test_a_json_string_with_real_newlines_prints_no_name(self, tmp_path):
        raw = (
            f'{{\n  "k": "-----BEGIN X-----\n{FRAG}=1\n{FRAG}2=\n'
            '-----END X-----"\n}\nNEXT=1\n'
        ).encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        # `{`, the opening line, two body lines, the closing line and `}`.
        assert lines == ["NEXT  ******** (1 chars)", "(6 line(s) not shown)"]
        _no_fragment(names, lines)

    def test_a_block_opened_mid_line_is_closed_by_its_own_label(self, tmp_path):
        raw = (
            f'x "-----BEGIN A-----\n-----END B-----\n{FRAG}=1\n-----END A-----\nNEXT=1\n'
        ).encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("NEXT",)
        assert lines == ["NEXT  ******** (1 chars)", "(4 line(s) not shown)"]
        _no_fragment(names, lines)

    def test_a_comment_that_mentions_a_marker_opens_nothing(self, tmp_path):
        raw = b"# paste the -----BEGIN X----- block below\nA=1\nB=2\n"
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "B")
        assert lines == ["A  ******** (1 chars)", "B  ******** (1 chars)"]

    # ---- an opener glued after unquoted text ----------------------------------

    def test_an_opener_glued_after_text_hands_the_doubt_on(self, tmp_path):
        # `A` is a plain assignment (no reader joins what follows) and stays
        # shown, but the next line is the block's body and would print as a name.
        value = "x-----BEGIN X-----"
        raw = f"A={value}\n{FRAG}=\n-----END X-----\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "NEXT")
        assert lines == [
            f"A  ******** ({len(value)} chars)",
            "NEXT  ******** (1 chars)",
            "(2 line(s) not shown)",
        ]
        _no_fragment(names, lines)

    def test_a_base64_body_line_after_a_glued_opener_is_not_a_name(self, tmp_path):
        body = "Ab3" * 14 + "x"
        assert len(body) == 43
        raw = f"A=x-----BEGIN X-----\n{body}=\n-----END X-----\nNEXT=1\n".encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A", "NEXT")
        assert lines[-1] == "(2 line(s) not shown)"
        assert body[:8] not in "\n".join([*names, *lines])

    def test_a_quoted_value_that_contains_a_marker_hands_no_doubt_on(self, tmp_path):
        # The quote closes the value: nothing of it can run onto the next line.
        names, lines = _read_env(tmp_path, b'A="x-----BEGIN"\nDEBUG=\n')
        assert names == ("A", "DEBUG")
        assert lines == ["A  ******** (11 chars)", "DEBUG  ******** (0 chars)"]

    # ---- lines an open quote or block swallows are counted -------------------

    def test_the_lines_an_adjacent_quote_swallows_are_counted(self, tmp_path):
        # `"v"` closes, `#it` is plain text, and the `'` of `it's` opens a run
        # that never closes: it eats the rest of the file.
        raw = b'A="v"#it\'s\nSECRET_TAIL=zzz\nB=2\n'
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A",)
        assert lines[0].startswith("A  ")
        assert lines[1:] == ["(2 line(s) not shown)"]
        assert "SECRET_TAIL" not in "\n".join(lines)

    @pytest.mark.parametrize(
        ("raw", "swallowed"),
        [
            (b'A="open\nX=1\nY=2\n', 2),
            (b"A='open\nX=1\n", 1),
            (b"A=`open\nX=1\n\nY=2", 2),
            (b"A=-----BEGIN P-----\nX=1\nY=2\n", 2),
            (b'A="open\n\n  \nX=1\n', 1),
            (b'A="open', 0),
        ],
        ids=["double", "single", "backtick", "armor", "blanks-not-counted", "nothing"],
    )
    def test_what_a_value_that_never_closes_swallows_is_counted(
        self, tmp_path, raw, swallowed
    ):
        names, lines = _read_env(tmp_path, raw)
        assert names == ("A",)
        assert lines[1:] == ([f"({swallowed} line(s) not shown)"] if swallowed else [])

    def test_a_value_that_closes_swallows_nothing(self, tmp_path):
        armor = "-----BEGIN P-----\nc\n-----END P-----"
        raw = f'K="a\nb"\nL={armor}\n'.encode()
        names, lines = _read_env(tmp_path, raw)
        assert names == ("K", "L")
        assert lines == [
            "K  ******** (3 chars)",
            f"L  ******** ({len(armor)} chars)",
        ]

    def test_blanks_after_a_closing_quote_on_a_later_line_swallow_nothing(
        self, tmp_path
    ):
        # The value ends at the blank that follows its close, which is on the
        # LAST line of a multi-line value: nothing was swallowed.
        raw = b'K="a\nb" # a note\nL=1\n'
        names, lines = _read_env(tmp_path, raw)
        assert names == ("K", "L")
        assert lines == ["K  ******** (3 chars)", "L  ******** (1 chars)"]

    # ---- only the screen is guarded ------------------------------------------

    def test_the_hand_off_and_the_seal_never_consult_the_display_scan(
        self, tmp_path, private_tmp, cloud_home, monkeypatch
    ):
        raw = b'A="v"#it\'s\nSECRET_TAIL=zzz\nB=2\n'
        # Built first: a push set's `names` are read through the scan.
        ps = _env_set(tmp_path, {".env": raw})
        sealed = nodes.push_set_digest(ps, "manual")
        changed = _env_set(tmp_path, {".env": raw.replace(b"zzz", b"zzy")})

        def refuse(text):
            raise AssertionError("the display scan decides what is SHOWN only")

        monkeypatch.setattr(nodes, "_dotenv_scan", refuse)
        handoff = nodes.write_manual_handoff(changed)
        # Every swallowed line travels, byte for byte.
        assert handoff.read_text(encoding="utf-8").endswith(
            raw.replace(b"zzz", b"zzy").decode()
        )
        # And the seal reads those lines too: change one and it no longer matches.
        assert nodes.push_set_digest(changed, "manual") != sealed


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

    def test_a_fifty_thousand_line_file(self):
        text = "".join(f"K{i}=v{i}\n" for i in range(50_000))
        entries, took = self._timed(text)
        assert len(entries) == 50_000
        assert took < self.BOUND_S

    def test_a_two_hundred_thousand_line_armored_block(self):
        text = (
            "K=-----BEGIN X-----\n" + "abcd=\n" * 200_000 + "-----END X-----\nNEXT=1\n"
        )
        entries, took = self._timed(text)
        assert [name for name, _ in entries] == ["K", "NEXT"]
        assert took < self.BOUND_S

    def test_a_block_that_never_ends_is_one_pass(self):
        entries, took = self._timed("K=-----BEGIN X-----\n" + "abcd=\n" * 200_000)
        assert [name for name, _ in entries] == ["K"]
        assert took < self.BOUND_S

    def test_a_hundred_thousand_armored_blocks_in_a_row(self):
        text = "-----BEGIN X-----\nabc=\n-----END X-----\n" * 100_000 + "NEXT=1\n"
        entries, took = self._timed(text)
        assert entries == [("NEXT", 1)]
        assert took < self.BOUND_S

    def test_three_hundred_thousand_adjacent_quoted_runs(self):
        entries, took = self._timed("A=" + "'a'" * 300_000 + "\nB=1\n")
        assert entries == [("A", 300_000), ("B", 1)]
        assert took < self.BOUND_S

    def test_a_million_character_name_that_is_not_one(self):
        entries, took = self._timed("A" * 1_000_000 + "=1\nB=2\n")
        assert entries == [("B", 1)]
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


def _dispatch(
    monkeypatch,
    fake_platform,
    cfg_path,
    *,
    tool="claude",
    running=False,
    live=False,
    refusal=None,
    dry_run=False,
    tile_only=False,
    which=0,
):
    cfg = load_config(cfg_path)
    windows: list = []
    colors: dict = {}
    targets: list = []
    asked: list[str] = []
    monkeypatch.setattr(
        "magent.launch.cloud_refusal",
        lambda config, sid: (asked.append(sid), refusal)[1],
    )
    # THE liveness answer (psmux.live_sessions), not a per-project has-session.
    monkeypatch.setattr(
        "magent.psmux.live_sessions",
        lambda names, *a, **kw: list(names) if live else [],
    )
    n = launch._dispatch_cli_agent_project(
        fake_platform,
        cfg,
        RunOpts(dry_run=dry_run, tile_only=tile_only),
        cfg.projects[which],
        tool,
        False,
        None,
        cfg.settings.tools,
        True,
        lambda key, mode: running,
        targets,
        windows,
        colors,
    )
    return n, windows, asked, targets, colors


class TestTheLaunchPathCreatesACloudPaneOnce:
    def test_a_new_cloud_project_collects_one_pane_typed_once_and_branded(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        n, windows, _, targets, colors = _dispatch(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path, color="blue"),
        )
        assert n == 1
        [w] = windows
        assert w.command == 'claude --cloud "Fix the login bug"'
        assert (w.resend, w.nick) == (False, "cloud")
        assert w.window_name == "api" and w.cwd == str(tmp_path / "api")
        assert colors == {"api": "blue"}
        [t] = targets
        assert (t.key, t.is_new) == ("api", True)

    def test_a_windows_list_cannot_make_it_more_than_one_pane(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # `windows` names several panes of a LOCAL agent project; a cloud
        # project is one session, named by the project, never by a window.
        path = _cloud_cfg(tmp_config, tmp_path, windows=["left", "right"])
        n, windows, _, targets, _ = _dispatch(monkeypatch, fake_platform, path)
        assert n == 1
        assert [w.window_name for w in windows] == ["api"]
        assert [t.key for t in targets] == ["api"]

    def test_a_refused_create_is_skipped_with_its_reason(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        n, windows, _, targets, _ = _dispatch(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            refusal="main has unpushed commits",
        )
        assert (n, windows, targets) == (0, [], [])
        assert "SKIP: api — main has unpushed commits" in capsys.readouterr().out

    def test_a_live_session_is_reattached_without_asking_the_gate(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # Closed window + live session: the pane is collected for attach, the
        # create is skipped by the bring-up's own has-session probe, and
        # nothing new is created -- so a dirty tree must not stop the user
        # getting their window back.
        _, windows, asked, _, _ = _dispatch(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            live=True,
            refusal="dirty",
        )
        assert asked == []
        assert len(windows) == 1

    def test_an_open_window_is_neither_gated_nor_collected(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, _cloud_cfg(tmp_config, tmp_path), running=True
        )
        assert (n, windows, asked) == (0, [], [])
        # Still tiled: it is open, just not created again.
        assert [(t.key, t.is_new) for t in targets] == [("api", False)]

    def test_a_retile_creates_and_gates_nothing_but_still_tiles(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        _, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, _cloud_cfg(tmp_config, tmp_path), tile_only=True
        )
        assert (windows, asked) == ([], [])
        assert len(targets) == 1

    def test_a_dry_run_prints_the_command_and_asks_nothing(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        _, windows, asked, _, _ = _dispatch(
            monkeypatch, fake_platform, _cloud_cfg(tmp_config, tmp_path), dry_run=True
        )
        assert (windows, asked) == ([], [])
        out = capsys.readouterr().out
        # The gate reads git; a preview does not, and must not read as approval.
        assert (
            'would run: claude --cloud "Fix the login bug" (create gate not consulted)'
            in out
        )
        assert "[@cloud]" in out

    def test_a_dry_run_of_an_open_window_previews_nothing(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # A dry run says what the real run would do: for a window that is
        # already open that is nothing, and "would run" would read as a second
        # cloud session.
        _dispatch(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            dry_run=True,
            running=True,
        )
        assert "would run" not in capsys.readouterr().out

    def test_a_cloud_project_with_no_task_is_skipped_by_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # It loads (J1), but nothing can be created from it.
        n, windows, _, _, _ = _dispatch(
            monkeypatch, fake_platform, _cloud_cfg(tmp_config, tmp_path, cloudTask=None)
        )
        assert (n, windows) == (0, [])
        assert 'no "cloudTask" set' in capsys.readouterr().out

    def test_without_psmux_a_cloud_project_is_skipped_not_opened_in_a_plain_terminal(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # A plain terminal has no has-session dedupe: every --go after the
        # window was closed would create another cloud session.
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        launch._dispatch_cli_agent_project(
            fake_platform,
            cfg,
            RunOpts(),
            cfg.projects[0],
            "claude",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda k, m: False,
            [],
            [],
            {},
        )
        out = capsys.readouterr().out
        # The words doctor repeats verbatim: one constant, not two copies.
        assert f"SKIP: api — {launch.CLOUD_NEEDS_PSMUX}" in out
        assert "cloud projects run in a psmux pane" in launch.CLOUD_NEEDS_PSMUX
        assert fake_platform.launched_terminals == []
        assert fake_platform.psmux_launches == []

    @pytest.mark.parametrize(
        ("setting", "platform", "skipped"),
        [
            (False, True, True),
            (True, False, True),
            (False, False, True),
            (True, True, False),
        ],
        ids=["setting-off", "no-platform-psmux", "both-off", "both-on"],
    )
    def test_the_launch_loop_needs_the_setting_and_the_platform_for_a_cloud_pane(
        self, fake_platform, tmp_config, tmp_path, capsys, setting, platform, skipped
    ):
        fake_platform._supports_psmux = platform
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, settings={"psmux": setting}))
        assert launch.launch_uses_psmux(cfg, fake_platform) is (setting and platform)

        launch._launch_projects(
            fake_platform, cfg, RunOpts(dry_run=True), cfg.projects, None
        )

        out = capsys.readouterr().out
        assert (f"SKIP: api — {launch.CLOUD_NEEDS_PSMUX}" in out) is skipped
        assert ("would run" in out) is not skipped

    def test_a_cloud_project_is_never_typed_a_continue_command(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # The loop is what routes it: a cloud project is not `runs_on_node`, so
        # without the cloud branch it fell through to the cli-agent dispatch
        # and was typed `claude --continue`.
        fake_platform._supports_psmux = True
        monkeypatch.setattr("magent.launch.cloud_refusal", lambda config, sid: None)
        monkeypatch.setattr("magent.psmux.live_sessions", lambda names, *a, **kw: [])
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, settings={"psmux": True}))
        result = launch._launch_projects(
            fake_platform, cfg, RunOpts(), cfg.projects, None
        )
        assert [w.command for w in result.psmux_windows] == [
            'claude --cloud "Fix the login bug"'
        ]


def _twin_cfg(tmp_config, tmp_path: Path, *, cloud_first=False) -> str:
    """A local project and a cloud one for the SAME folder, so one session
    name: the gate (looked up by that name) can only ever read one of them."""
    folder = tmp_path / "api"
    folder.mkdir(exist_ok=True)
    local: dict[str, object] = {"path": str(folder)}
    cloud: dict[str, object] = {
        "path": str(folder),
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {"psmux": True},
            "projects": [cloud, local] if cloud_first else [local, cloud],
        }
    )


class TestTwoProjectsOneSessionNameNeverLetACloudCreateSlipTheGate:
    @pytest.mark.parametrize("live", [False, True])
    def test_the_cloud_twin_of_an_earlier_project_is_skipped_by_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys, live
    ):
        # The gate is asked by session name and answers for the FIRST enabled
        # project with it: here the local one, a non-cloud project it waves
        # through. Creating the cloud pane on that answer would skip the git
        # and .env checks, so the dispatch refuses the cloud twin instead --
        # live session or not; the other project's own dispatch owns the pane.
        path = _twin_cfg(tmp_config, tmp_path)
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, path, which=1, live=live
        )
        out = capsys.readouterr().out
        assert (n, windows, asked, targets) == (0, [], [], [])
        assert out.count("SKIP:") == 1
        assert "SKIP: api — another enabled project uses the session name api" in out
        assert "rename one (set a title)" in out

    def test_the_first_project_with_the_name_is_not_refused(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # Cloud first: the gate reads the very project being created, so the
        # duplicate rule has nothing to refuse here.
        path = _twin_cfg(tmp_config, tmp_path, cloud_first=True)
        n, [w], asked, _, _ = _dispatch(monkeypatch, fake_platform, path, which=0)
        assert (n, asked) == (1, ["api"])
        assert w.nick == "cloud" and w.resend is False
        assert "SKIP" not in capsys.readouterr().out

    def test_a_title_tells_the_twins_apart(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        folder = tmp_path / "api"
        folder.mkdir()
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [
                    {"path": str(folder)},
                    {
                        "path": str(folder),
                        "title": "api cloud",
                        "node": "cloud",
                        "cloudTask": "t",
                    },
                ],
            }
        )
        n, [w], asked, _, _ = _dispatch(monkeypatch, fake_platform, path, which=1)
        assert (n, asked, w.window_name) == (1, ["api-cloud"], "api-cloud")
        assert "SKIP" not in capsys.readouterr().out

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_two_identical_cloud_entries_are_one_window_and_one_skip(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys, dry_run
    ):
        # Entries identical down to the colour (load backfills a distinct one
        # when none is given) compare equal, so the twin check passes both; the
        # second must still not queue a second `claude --cloud` (a billed
        # duplicate) under the same session name, nor a second tile target.
        monkeypatch.setattr("magent.launch.cloud_refusal", lambda config, sid: None)
        monkeypatch.setattr("magent.psmux.live_sessions", lambda names, *a, **kw: [])
        fake_platform._supports_psmux = True
        folder = tmp_path / "api"
        folder.mkdir()
        entry = {
            "path": str(folder),
            "node": "cloud",
            "cloudTask": "t",
            "color": "blue",
        }
        cfg = load_config(
            tmp_config(
                {
                    "version": SCHEMA_VERSION,
                    "settings": {"psmux": True},
                    "projects": [dict(entry), dict(entry)],
                }
            )
        )
        result = launch._launch_projects(
            fake_platform, cfg, RunOpts(dry_run=dry_run), cfg.projects, None
        )
        out = capsys.readouterr().out
        assert out.count("SKIP:") == 1
        assert "SKIP: api — already queued under session api" in out
        assert "(duplicate project entry)" in out
        assert [t.key for t in result.targets] == ["api"]
        assert len(result.psmux_windows) == (0 if dry_run else 1)
        assert out.count("would run") == (1 if dry_run else 0)

    def test_the_launch_loop_keeps_the_local_pane_and_skips_the_cloud_twin(
        self, fake_platform, tmp_config, tmp_path, capsys
    ):
        fake_platform._supports_psmux = True
        cfg = load_config(_twin_cfg(tmp_config, tmp_path))
        result = launch._launch_projects(
            fake_platform, cfg, RunOpts(), cfg.projects, None
        )
        out = capsys.readouterr().out
        assert out.count("SKIP:") == 1 and "rename one" in out
        [w] = result.psmux_windows
        assert w.nick is None and w.resend is True
        assert "--cloud" not in w.command


class TestACloudProjectOnTheWrongToolIsSkippedOnce:
    """The gate refuses it too (``TestTheCreateGate``); the launch path must
    not rely on ``cloud_pane_command`` alone, which would type
    ``bash --cloud`` for ``bash -c "claude ..."``."""

    def test_another_tool_is_one_skip_line_naming_the_tool(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path, tool="codex"),
            tool="codex",
        )
        out = capsys.readouterr().out
        assert (n, windows, asked, targets) == (0, [], [], [])
        assert out.count("SKIP:") == 1
        assert "SKIP: api — " in out and "'codex'" in out

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_wrapper_around_claude_is_one_skip_line_not_a_bash_pane(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys, dry_run
    ):
        path = _cloud_cfg(
            tmp_config,
            tmp_path,
            settings={"tools": {"claude": 'bash -c "claude --continue"'}},
        )
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, path, dry_run=dry_run
        )
        out = capsys.readouterr().out
        assert (n, windows, asked, targets) == (0, [], [], [])
        assert out.count("SKIP:") == 1
        assert "'bash'" in out
        assert "would run" not in out

    def test_an_executable_it_cannot_type_is_one_skip_line(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # claude by name, but the path holds a cmd.exe metacharacter.
        path = _cloud_cfg(
            tmp_config,
            tmp_path,
            settings={"tools": {"claude": r"C:\a&b\claude.exe"}},
        )
        n, windows, _, _, _ = _dispatch(monkeypatch, fake_platform, path)
        out = capsys.readouterr().out
        assert (n, windows) == (0, [])
        assert out.count("SKIP:") == 1 and "cannot type" in out

    def test_a_wrong_tool_is_named_before_a_missing_task_everywhere(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # tool -> task -> git -> push set, in the dispatch and in the gate alike.
        path = _cloud_cfg(tmp_config, tmp_path, tool="codex", cloudTask=None)
        real_gate = launch.cloud_refusal  # `_dispatch` stubs the module attribute
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, path, tool="codex"
        )
        out = capsys.readouterr().out
        assert (n, windows, asked, targets) == (0, [], [], [])
        assert out.count("SKIP:") == 1
        assert "'codex'" in out and "cloudTask" not in out
        cfg = load_config(path)
        refusal = real_gate(cfg, "api") or ""
        assert "'codex'" in refusal and refusal != launch.NO_CLOUD_TASK
        # The project's OWN tool, in the one ladder's words.
        assert refusal == launch.cloud_tool_refusal(
            "codex", cfg.settings.tools.get("codex")
        )

    @pytest.mark.parametrize("dry_run", [False, True])
    @pytest.mark.parametrize(
        ("proj", "settings"),
        [({"tool": "code"}, None), ({}, {"defaultTool": "cursor"})],
        ids=["own-tool", "inherited-default-tool"],
    )
    def test_an_ide_tool_is_one_skip_line_and_opens_nothing(
        self, fake_platform, tmp_config, tmp_path, capsys, proj, settings, dry_run
    ):
        # The user asked for a cloud session and an IDE hosts none: the loop
        # must refuse it, not open a local IDE window.
        fake_platform._supports_psmux = True
        merged = {"psmux": True, **(settings or {})}
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, settings=merged, **proj))
        result = launch._launch_projects(
            fake_platform, cfg, RunOpts(dry_run=dry_run), cfg.projects, None
        )
        out = capsys.readouterr().out
        assert out.count("SKIP:") == 1
        assert "SKIP: api — " in out and "claude --cloud" in out
        assert fake_platform.launched_vscode == []
        assert fake_platform.launched_terminals == []
        assert fake_platform.psmux_launches == []
        assert (result.psmux_windows, result.targets) == ([], [])

    def test_an_ide_project_that_is_not_cloud_still_opens_its_ide(
        self, fake_platform, tmp_config, tmp_path
    ):
        # The refusal is the cloud project's alone.
        folder = tmp_path / "api"
        folder.mkdir()
        cfg = load_config(
            tmp_config(
                {
                    "version": SCHEMA_VERSION,
                    "projects": [{"path": str(folder), "tool": "code"}],
                }
            )
        )
        launch._launch_projects(fake_platform, cfg, RunOpts(), cfg.projects, None)
        assert len(fake_platform.launched_vscode) == 1

    def test_claude_at_a_path_is_accepted(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        path = _cloud_cfg(
            tmp_config,
            tmp_path,
            settings={"tools": {"claude": r"C:\tools\Claude.EXE --continue"}},
        )
        _, [w], _, _, _ = _dispatch(monkeypatch, fake_platform, path)
        assert w.command == r'C:\tools\Claude.EXE --cloud "Fix the login bug"'


class TestTheCloudToolRule:
    @pytest.mark.parametrize(
        "command",
        [
            "claude",
            "claude --continue",
            "claude.exe",
            "claude.cmd",
            "CLAUDE.EXE",
            "Claude.Cmd --continue",
            r"C:\tools\claude.exe --continue",
            r"C:\npm\claude.cmd",
            "/usr/local/bin/claude --continue",
            "./claude",
        ],
    )
    def test_claude_by_any_path_case_or_windows_extension_is_accepted(self, command):
        assert launch.cloud_tool_refusal("claude", command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c claude",
            'bash -c "claude --continue"',
            "cmd /c claude",
            "node claude.js",
            "python -m claude",
            "claude-wrapper",
            "claudex",
            "myclaude.exe",
            "claude.sh",
            "claude.py",
            "claude.exe.bak",
            "claude.exe.cmd",
            r"C:\tools\notclaude.exe",
            r"C:\claude\tool.exe",
            "claude/",
        ],
    )
    def test_anything_else_in_the_executable_slot_is_refused_by_name(self, command):
        refusal = launch.cloud_tool_refusal("claude", command)
        assert refusal is not None
        assert repr(command.split()[0]) in refusal

    @pytest.mark.parametrize("command", [None, "", "   "])
    def test_a_claude_tool_with_no_command_is_an_unknown_tool(self, command):
        refusal = launch.cloud_tool_refusal("claude", command)
        assert refusal is not None and "unknown tool 'claude'" in refusal

    @pytest.mark.parametrize("tool", ["codex", "cursor-agent", "agy", "Claude", "code"])
    def test_a_tool_that_is_not_claude_is_refused_whatever_its_command(self, tool):
        # Even `claude` as its command: the tool decides what the project is.
        refusal = launch.cloud_tool_refusal(tool, "claude")
        assert refusal is not None and repr(tool) in refusal
        assert "claude --cloud" in refusal


class TestTheOneCloudCommandLadder:
    """``launch.cloud_command`` is the ONE ladder -- tool, then task, then the
    command a pane can safely be typed -- that ``--go``, ``up`` and `doctor` all
    read, so no surface words a refusal or orders two of them differently."""

    TOOL_REFUSAL = launch.cloud_tool_refusal("codex", "codex")

    def test_a_good_cloud_project_gets_its_pane_command_and_no_reason(self):
        assert launch.cloud_command("claude", "claude --continue", "Fix it") == (
            'claude --cloud "Fix it"',
            "",
        )

    def test_the_tool_is_asked_first_whatever_else_is_missing(self):
        assert self.TOOL_REFUSAL is not None
        assert launch.cloud_command("codex", "codex", None) == (
            "",
            self.TOOL_REFUSAL,
        )

    @pytest.mark.parametrize("command", [None, "", "  "])
    def test_a_tool_with_no_command_is_the_unknown_tool_reason(self, command):
        cmd, why = launch.cloud_command("claude", command, "Fix it")
        assert cmd == "" and "unknown tool 'claude'" in why

    @pytest.mark.parametrize("task", [None, ""])
    def test_a_missing_task_comes_next_and_before_the_typing_check(self, task):
        # `~/bin/claude` could not be typed either, but the task is first.
        assert launch.cloud_command("claude", "claude", task) == (
            "",
            launch.NO_CLOUD_TASK,
        )
        assert launch.cloud_command("claude", "~/bin/claude", task) == (
            "",
            launch.NO_CLOUD_TASK,
        )

    def test_what_the_pane_cannot_type_is_said_in_the_builders_own_words(self):
        from magent.sessions.claude import cloud_pane_command

        with pytest.raises(ValueError, match="cannot type claude executable") as exe:
            cloud_pane_command("~/bin/claude --continue", "Fix it")
        with pytest.raises(ValueError, match="unsafe cloud task") as task:
            cloud_pane_command("claude", "fix it; rm everything")

        assert launch.cloud_command("claude", "~/bin/claude --continue", "Fix it") == (
            "",
            str(exe.value),
        )
        assert launch.cloud_command("claude", "claude", "fix it; rm everything") == (
            "",
            str(task.value),
        )

    def test_the_bring_up_row_carries_the_ladders_answer(self, tmp_config, tmp_path):
        from magent import psmux

        for tool, task, tools in (
            ("claude", "Fix it", {"claude": "claude --continue"}),
            ("claude", None, {"claude": "claude"}),
            ("claude", "Fix it", {"claude": "~/bin/claude"}),
            ("claude", "Fix it", {"claude": "bash -c claude"}),
            # The project's OWN tool is what the ladder is asked about.
            ("codex", "Fix it", {"claude": "claude", "codex": "codex"}),
        ):
            cfg = load_config(
                _cloud_cfg(
                    tmp_config,
                    tmp_path,
                    cloudTask=task,
                    tool=tool,
                    settings={"tools": tools},
                )
            )
            [row] = psmux.eligible_projects(cfg)
            assert (row["cmd"], row.get("cmd_why", "")) == launch.cloud_command(
                tool, tools[tool], task
            )


class TestOneSessionNamingRule:
    """A session id is ``psmux.session_name(title or leaf)``: the rule
    ``psmux.eligible_projects`` names sessions by. The cloud helpers read it
    through ``nodes.node_sid`` and never spell it a second time."""

    def _cfg(self, tmp_config, tmp_path: Path):
        projects = []
        for i, title in enumerate(("My App v1.2", "a:b c", None)):
            folder = tmp_path / f"dir{i}"
            folder.mkdir()
            entry: dict[str, object] = {"path": str(folder), "node": "cloud"}
            if title is not None:
                entry["title"] = title
            projects.append(entry)
        return load_config(
            tmp_config({"version": SCHEMA_VERSION, "projects": projects})
        )

    def test_the_cloud_sessions_are_the_ones_the_bring_up_names(
        self, tmp_config, tmp_path
    ):
        cfg = self._cfg(tmp_config, tmp_path)
        rows = psmux.eligible_projects(cfg)
        assert {r["session"] for r in rows} == {"My-App-v1-2", "a-b-c", "dir2"}
        # The gate and the shadow helpers name a project by `node_sid`; the
        # bring-up names its row by the same rule.
        for row, proj in zip(rows, cfg.projects, strict=True):
            assert nodes.node_sid(proj) == row["session"]

    def test_a_disabled_or_ide_cloud_project_is_still_named_by_the_same_rule(
        self, tmp_config, tmp_path
    ):
        # The bring-up lists only the enabled CLI-agent projects; `node_sid`
        # names every project. The ones the bring-up skips must still carry
        # exactly the name it WOULD have given them, or a refusal or a doctor
        # row for an enabled IDE project would point at a session that is not
        # theirs.
        specs: list[tuple[str, dict[str, object]]] = [
            ("Off Proj v1.0", {"enabled": False}),
            ("ide:proj", {"tool": "code"}),
            ("Live One", {}),
        ]
        projects = []
        for i, (title, extra) in enumerate(specs):
            folder = tmp_path / f"dir{i}"
            folder.mkdir()
            projects.append(
                {
                    "path": str(folder),
                    "title": title,
                    "node": "cloud",
                    "cloudTask": "t",
                    **extra,
                }
            )
        cfg = load_config(tmp_config({"version": SCHEMA_VERSION, "projects": projects}))
        assert {str(r["session"]) for r in psmux.eligible_projects(cfg)} == {"Live-One"}
        for (title, _), proj in zip(specs, cfg.projects, strict=True):
            assert nodes.node_sid(proj) == psmux.session_name(title)
        assert [nodes.node_sid(p) for p in cfg.projects] == [
            "Off-Proj-v1-0",
            "ide-proj",
            "Live-One",
        ]

    def test_a_session_id_finds_its_project(self, tmp_config, tmp_path):
        cfg = self._cfg(tmp_config, tmp_path)
        for row in psmux.eligible_projects(cfg):
            proj = launch.project_for_session(cfg, str(row["session"]))
            assert proj is not None and proj.path == row["path"]
        assert launch.project_for_session(cfg, "nope") is None

    @pytest.mark.parametrize("title", ["My App v1.2", "a:b c", "plain"])
    def test_the_pane_the_launch_collects_is_named_by_the_same_rule(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, title
    ):
        # The dispatch names the window from the title it generated; the gate
        # looks the project up by `node_sid`. They must agree, or the gate
        # would be asked about a session no project owns and wave it through.
        path = _cloud_cfg(tmp_config, tmp_path, title=title)
        _, [w], asked, _, _ = _dispatch(monkeypatch, fake_platform, path)
        proj = load_config(path).projects[0]
        assert w.window_name == nodes.node_sid(proj)
        assert asked == [w.window_name]
        assert launch.project_for_session(load_config(path), w.window_name) is not None

    def test_a_disabled_project_never_stands_for_the_session(
        self, tmp_config, tmp_path, monkeypatch
    ):
        # The user turns an old entry off and adds a cloud one for the same
        # folder. The bring-up only ever sees the enabled one, so the gate
        # must too -- else it would read the old entry, find no cloud project
        # and let the create through ungated.
        folder = tmp_path / "api"
        folder.mkdir()
        cfg = load_config(
            tmp_config(
                {
                    "version": SCHEMA_VERSION,
                    "projects": [
                        {"path": str(folder), "enabled": False},
                        {"path": str(folder), "node": "cloud", "cloudTask": "t"},
                    ],
                }
            )
        )
        proj = launch.project_for_session(cfg, "api")
        assert proj is not None and proj.node == "cloud"
        monkeypatch.setattr(
            "magent.launch.node_git_states",
            lambda config, p: [_state(folder, dirty=True)],
        )
        assert "uncommitted" in (launch.cloud_refusal(cfg, "api") or "")


class TestTheCreateGate:
    def _inputs(self, monkeypatch, tmp_config, tmp_path, *, env=False, **state_kw):
        """A cloud config and the git read the gate will see: one repo (clean
        and pushed unless ``state_kw`` says otherwise) and, with ``env``, an
        ignored ``.env`` holding a secret."""
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        repo = tmp_path / "api"
        if env:
            (repo / ".env").write_text("API_TOKEN=hunter2-secret\n", encoding="utf-8")
            state_kw.setdefault("ignored", (".env",))
        state = _state(repo, **state_kw)
        monkeypatch.setattr(
            "magent.launch.node_git_states", lambda config, proj: [state]
        )
        return cfg, state

    def test_it_reads_the_one_repository_and_refuses_a_dirty_tree(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg, _ = self._inputs(monkeypatch, tmp_config, tmp_path, dirty=True)
        assert "uncommitted" in (launch.cloud_refusal(cfg, "api") or "")

    @pytest.mark.parametrize(
        ("kw", "phrase"),
        [
            ({"unpushed": True}, "unpushed"),
            ({"url": "https://gitlab.com/me/api.git"}, "not on GitHub"),
            ({"url": ""}, "no 'origin' remote"),
            ({"detached": True}, "detached"),
            ({"no_commits": True}, "no commits"),
        ],
    )
    def test_each_unclonable_checkout_is_refused_by_the_cloud_check(
        self, monkeypatch, tmp_config, tmp_path, kw, phrase
    ):
        cfg, _ = self._inputs(monkeypatch, tmp_config, tmp_path, **kw)
        assert phrase in (launch.cloud_refusal(cfg, "api") or "")

    def test_a_workspace_of_several_repositories_is_refused(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        monkeypatch.setattr(
            "magent.launch.node_git_states",
            lambda config, proj: [_state(tmp_path / "a"), _state(tmp_path / "b")],
        )
        assert "ONE repository" in (launch.cloud_refusal(cfg, "api") or "")

    def test_a_folder_that_is_not_a_repository_is_refused(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [])
        assert "not a git repository" in (launch.cloud_refusal(cfg, "api") or "")

    def test_git_that_fails_is_a_named_refusal_not_a_traceback(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))

        def _git_fails(config, proj):
            raise RemoteError(
                128, "fatal: detected dubious ownership", ("git", "status")
            )

        monkeypatch.setattr("magent.launch.node_git_states", _git_fails)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert "git could not read" in refusal
        assert "dubious ownership" in refusal

    def test_git_that_never_answers_is_still_a_named_refusal(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))

        def _git_hangs(config, proj):
            raise RemoteError(None, "", ("git", "status"), timed_out=True)

        monkeypatch.setattr("magent.launch.node_git_states", _git_hangs)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert refusal.startswith("git could not read")
        assert refusal.endswith(": no answer") and "None" not in refusal

    def test_git_that_exits_without_a_word_names_its_exit_code(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))

        def _git_silent(config, proj):
            raise RemoteError(129, "  \n", ("git", "status"))

        monkeypatch.setattr("magent.launch.node_git_states", _git_silent)
        assert (launch.cloud_refusal(cfg, "api") or "").endswith(": exit 129")

    def test_git_says_its_last_line_and_nothing_a_terminal_would_obey(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))

        def _git_noisy(config, proj):
            raise RemoteError(
                128, "hint: first\nfatal: \x1b[31mnot a repository", ("git", "status")
            )

        monkeypatch.setattr("magent.launch.node_git_states", _git_noisy)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert refusal.endswith(": fatal: ?[31mnot a repository")
        assert "hint: first" not in refusal and "\x1b" not in refusal

    def test_a_folder_this_user_cannot_read_is_a_named_refusal(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))

        def _denied(config, proj):
            raise PermissionError(13, "denied")

        monkeypatch.setattr("magent.launch.node_git_states", _denied)
        assert "PermissionError" in (launch.cloud_refusal(cfg, "api") or "")

    def test_a_missing_task_is_refused_before_git_is_read(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=None))

        def _boom(config, proj):
            raise AssertionError(
                "git must not be read for a project that cannot be created"
            )

        monkeypatch.setattr("magent.launch.node_git_states", _boom)
        assert launch.cloud_refusal(cfg, "api") == launch.NO_CLOUD_TASK

    @pytest.mark.parametrize(
        ("proj", "settings", "named"),
        [
            ({"tool": "codex"}, None, "'codex'"),
            ({}, {"defaultTool": "codex"}, "'codex'"),
            ({"tool": "claude"}, {"tools": {"claude": 'bash -c "claude"'}}, "'bash'"),
            (
                {},
                {"tools": {"claude": "claude-wrapper --continue"}},
                "'claude-wrapper'",
            ),
        ],
        ids=["own-tool", "inherited-default-tool", "wrapper-command", "other-exe"],
    )
    def test_a_tool_that_is_not_claude_is_refused_before_git_is_read(
        self, monkeypatch, tmp_config, tmp_path, proj, settings, named
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, settings=settings, **proj))

        def _boom(config, p):
            raise AssertionError(
                "git must not be read for a project that cannot be created"
            )

        monkeypatch.setattr("magent.launch.node_git_states", _boom)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert named in refusal and "claude --cloud" in refusal

    def test_claude_at_a_path_is_past_the_tool_check(
        self, monkeypatch, tmp_config, tmp_path
    ):
        cfg = load_config(
            _cloud_cfg(
                tmp_config,
                tmp_path,
                settings={"tools": {"claude": r"C:\tools\claude.cmd --continue"}},
            )
        )
        monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [])
        # Past the tool check, it reaches git -- and stops on the empty read.
        assert "not a git repository" in (launch.cloud_refusal(cfg, "api") or "")

    def test_a_clean_checkout_waits_on_the_push_set(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg, _ = self._inputs(monkeypatch, tmp_config, tmp_path, env=True)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert "magent node push api" in refusal and "API_TOKEN" in refusal
        assert "hunter2-secret" not in refusal

    def test_a_clean_checkout_with_a_handed_off_push_set_is_let_through(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg, state = self._inputs(monkeypatch, tmp_config, tmp_path, env=True)
        ps = nodes.cloud_push_set(state.path, [state], home=tmp_path / "h")
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        assert launch.cloud_refusal(cfg, "api") is None

    def test_a_changed_secret_asks_for_the_hand_off_again(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg, state = self._inputs(monkeypatch, tmp_config, tmp_path, env=True)
        ps = nodes.cloud_push_set(state.path, [state], home=tmp_path / "h")
        nodes.write_cloud_record(
            "api", digest=nodes.push_set_digest(ps, "manual"), mode="manual"
        )
        (state.path / ".env").write_text("API_TOKEN=rotated\n", encoding="utf-8")
        assert "magent node push api" in (launch.cloud_refusal(cfg, "api") or "")

    def test_a_clean_checkout_with_nothing_to_hand_off_is_let_through(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg, _ = self._inputs(monkeypatch, tmp_config, tmp_path)
        assert launch.cloud_refusal(cfg, "api") is None

    def test_a_session_that_is_not_cloud_is_never_gated(self, tmp_config, tmp_path):
        folder = tmp_path / "api"
        folder.mkdir()
        cfg = load_config(
            tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": str(folder)}]})
        )
        assert launch.cloud_refusal(cfg, "api") is None

    def test_a_session_no_project_owns_is_never_gated(self, tmp_config, tmp_path):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        assert launch.cloud_refusal(cfg, "nobody") is None


class TestTheGateNeverRaises:
    """Every OSError the checks can raise -- a lock that stays taken, a file
    that cannot be read -- is a refusal that names it, never a traceback out of
    ``--go`` or ``up``."""

    def _handed_off(self, monkeypatch, tmp_config, tmp_path):
        """A project with a recorded hand-off, so the gate has to re-digest."""
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        repo = tmp_path / "api"
        (repo / ".env").write_text("API_TOKEN=hunter2-secret\n", encoding="utf-8")
        state = _state(repo, ignored=(".env",))
        monkeypatch.setattr(
            "magent.launch.node_git_states", lambda config, proj: [state]
        )
        nodes.write_cloud_record("api", digest="0" * 64, mode="manual")
        return cfg

    def test_a_lock_that_stays_taken_asks_the_user_to_try_again(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        # The digest key is made under a lock (`_place_key`), and the digest is
        # asked for exactly when a record has to be checked against the files.
        cfg = self._handed_off(monkeypatch, tmp_config, tmp_path)

        def _busy(*args, **kwargs):
            raise LockHeld("held by another writer")

        monkeypatch.setattr(nodes, "_digest_key", _busy)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        # Neutral on purpose: the lock held may be the digest key's, not the
        # records'.
        assert "another magent is updating cloud hand-off state; try again" in refusal
        assert "held by another writer" not in refusal

    def test_an_unreadable_push_file_names_the_file_and_the_repair(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg = self._handed_off(monkeypatch, tmp_config, tmp_path)

        def _unreadable(ps, mode):
            raise nodes.PushSetUnreadable(".env", "PermissionError")

        monkeypatch.setattr(nodes, "push_set_digest", _unreadable)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert ".env" in refusal and "PermissionError" in refusal
        assert "magent node push api" in refusal
        assert "hunter2-secret" not in refusal

    def test_an_unreadable_file_that_escapes_the_check_is_named_too(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg = self._handed_off(monkeypatch, tmp_config, tmp_path)

        def _escapes(sid, name, ps, recipient):
            raise nodes.PushSetUnreadable(".env.local", "PermissionError")

        monkeypatch.setattr(nodes, "cloud_env_refusal", _escapes)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert ".env.local" in refusal and "PermissionError" in refusal
        assert "magent node push api" in refusal

    def test_a_folder_the_push_set_cannot_list_is_named(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        cfg = self._handed_off(monkeypatch, tmp_config, tmp_path)

        def _cannot_list(*args, **kwargs):
            raise nodes.NodeConfigError("api: cannot be listed (PermissionError)")

        monkeypatch.setattr(nodes, "cloud_push_set", _cannot_list)
        assert "cannot be listed (PermissionError)" in (
            launch.cloud_refusal(cfg, "api") or ""
        )

    def test_any_other_os_error_is_named_by_class_and_never_by_its_text(
        self, monkeypatch, tmp_config, tmp_path, cloud_home
    ):
        # The OS's own words carry an absolute path (and so a user name).
        cfg = self._handed_off(monkeypatch, tmp_config, tmp_path)

        def _denied(*args, **kwargs):
            raise PermissionError(13, "denied", "C:/Users/someone/.magent/key")

        monkeypatch.setattr(nodes, "_digest_key", _denied)
        refusal = launch.cloud_refusal(cfg, "api") or ""
        assert "PermissionError" in refusal
        assert "someone" not in refusal and "denied" not in refusal


# ---------------------------------------------------------------------------
# J8: `magent up`, revive, the reaper and the session lists. Every path that
# could TYPE `claude --cloud` into a pane is a path that starts a new billed
# cloud session the CLI can neither list nor stop.
# ---------------------------------------------------------------------------


def _ide_twin_cfg(tmp_config, tmp_path: Path, *, cloud_first=False) -> str:
    """An IDE project and a cloud one for the SAME folder: config allows it (an
    IDE session and a pane are not twins), ``eligible_projects`` skips the IDE
    one, and ``project_for_session`` -- which the gate asks -- does not."""
    folder = tmp_path / "api"
    folder.mkdir(exist_ok=True)
    ide: dict[str, object] = {"path": str(folder), "tool": "code"}
    cloud: dict[str, object] = {
        "path": str(folder),
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {"psmux": True},
            "projects": [cloud, ide] if cloud_first else [ide, cloud],
        }
    )


class TestTheCloudPaneIsAPsmuxSession:
    def test_a_cloud_project_is_eligible_and_carries_the_cloud_command(
        self, tmp_config, tmp_path
    ):
        [entry] = psmux.eligible_projects(load_config(_cloud_cfg(tmp_config, tmp_path)))
        assert entry["cmd"] == 'claude --cloud "Fix the login bug"'
        assert entry["node"] == "cloud"
        assert "cmd_why" not in entry

    @pytest.mark.parametrize(
        ("proj", "settings", "said"),
        [
            ({"cloudTask": None}, None, launch.NO_CLOUD_TASK),
            ({"tool": "codex"}, None, "'codex'"),
            ({}, {"tools": {"claude": 'bash -c "claude --continue"'}}, "'bash'"),
            ({}, {"tools": {"claude": r"C:\a&b\claude.exe"}}, "cannot type"),
        ],
        ids=["no-task", "another-tool", "wrapper", "untypeable-executable"],
    )
    def test_a_cloud_project_that_cannot_start_a_session_has_no_command_and_says_why(
        self, tmp_config, tmp_path, proj, settings, said
    ):
        # Never `bash --cloud "t"`: an empty command is what every consumer
        # (bring_up, revive, the status table) already reads as "nothing to run".
        [entry] = psmux.eligible_projects(
            load_config(_cloud_cfg(tmp_config, tmp_path, settings=settings, **proj))
        )
        assert entry["cmd"] == ""
        assert said in str(entry["cmd_why"])
        assert entry["node"] == "cloud"

    def test_a_local_project_carries_no_node_and_no_reason(self, tmp_config, tmp_path):
        [entry] = psmux.eligible_projects(
            load_config(_cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None))
        )
        assert entry["node"] is None
        assert "cmd_why" not in entry

    def test_a_pool_project_is_still_not_a_local_psmux_session(
        self, tmp_config, tmp_path
    ):
        (tmp_path / "api").mkdir()
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"nodes": {"second": {"host": "box-second"}}},
                "projects": [{"path": str(tmp_path / "api"), "node": "second"}],
            }
        )
        assert psmux.eligible_projects(load_config(path)) == []

    def test_config_sessions_marks_the_cloud_row_and_only_that_one(
        self, tmp_config, tmp_path
    ):
        [row] = psmux.config_sessions(_cloud_cfg(tmp_config, tmp_path))
        assert row["node"] == "cloud"
        [local] = psmux.config_sessions(
            _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None)
        )
        assert local["node"] is None


class TestADownCloudPaneSaysWhy:
    """``status`` shows a project that is never probed with the one thing that
    keeps it from being probed; for a cloud pane "no agent command" would send
    the user after the wrong setting."""

    def _down(self, monkeypatch, path):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        _up, down, _all = psmux.psmux_status(load_config(path))
        return down

    def test_a_cloud_project_with_no_task_names_the_task(
        self, monkeypatch, tmp_config, tmp_path
    ):
        [entry] = self._down(
            monkeypatch, _cloud_cfg(tmp_config, tmp_path, cloudTask=None)
        )
        assert entry["reason"] == launch.NO_CLOUD_TASK

    def test_a_cloud_project_on_another_tool_names_the_tool(
        self, monkeypatch, tmp_config, tmp_path
    ):
        [entry] = self._down(
            monkeypatch, _cloud_cfg(tmp_config, tmp_path, tool="codex")
        )
        assert "'codex'" in str(entry["reason"])
        assert "no agent command" not in str(entry["reason"])

    def test_a_missing_folder_is_still_named_first(
        self, monkeypatch, tmp_config, tmp_path
    ):
        [entry] = self._down(
            monkeypatch, _cloud_cfg(tmp_config, tmp_path, path=str(tmp_path / "gone"))
        )
        assert entry["reason"] == "folder not found"

    def test_a_local_project_keeps_the_wording_it_had(
        self, monkeypatch, tmp_config, tmp_path
    ):
        [entry] = self._down(
            monkeypatch,
            _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None, tool="nosuch"),
        )
        assert entry["reason"] == "no agent command"


class TestReviveNeverRetypesACloudPane:
    """Revive types the pane's command into a pane at a shell. For a cloud pane
    that is a SECOND `claude --cloud` (a new billed session), so it is vetoed
    in every mode, with a reason, before anything is read or sent."""

    @pytest.fixture
    def sent(self, monkeypatch):
        out: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, **k: list(names))
        monkeypatch.setattr(psmux, "idle_sessions", lambda names, **k: set(names))
        monkeypatch.setattr(
            psmux, "send_keys", lambda sid, *a, **k: out.append(sid) or True
        )
        return out

    def test_a_pane_at_a_shell_is_not_re_typed_and_the_reason_says_why(
        self, sent, tmp_config, tmp_path
    ):
        why: dict[str, str] = {}
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        assert psmux.revive_sessions(cfg, only=["api"], vetoed=why) == []
        assert sent == []
        assert "cloud" in why["api"] and "second cloud session" in why["api"]

    def test_a_bulk_revive_has_the_same_veto(self, sent, tmp_config, tmp_path):
        why: dict[str, str] = {}
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        assert psmux.revive_sessions(cfg, vetoed=why) == []
        assert sent == [] and "cloud" in why["api"]

    def test_a_human_asking_for_a_parked_pane_back_is_vetoed_too(
        self, sent, tmp_config, tmp_path
    ):
        why: dict[str, str] = {}
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        revived = psmux.revive_sessions(
            cfg, only=["api"], resume_parked=True, vetoed=why
        )
        assert revived == [] and sent == []
        assert "cloud" in why["api"]

    def test_the_cloud_reason_beats_a_missing_command(self, sent, tmp_config, tmp_path):
        why: dict[str, str] = {}
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=None))
        assert psmux.revive_sessions(cfg, only=["api"], vetoed=why) == []
        assert "cloud" in why["api"] and "no command" not in why["api"]

    def test_the_same_project_as_a_local_one_is_still_revived(
        self, sent, tmp_config, tmp_path
    ):
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None))
        assert psmux.revive_sessions(cfg, only=["api"]) == ["api"]
        assert sent == ["api"]

    def test_status_prints_the_reason_for_r_n(
        self, sent, monkeypatch, capsys, tmp_config, tmp_path
    ):
        from pathlib import Path

        from magent.cli import status as status_mod

        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        monkeypatch.setattr(status_mod, "_load_config_or_exit", lambda path: cfg)
        status_mod._revive_session(Path("unused.json"), "api")
        out = capsys.readouterr().out
        assert "Did not revive api: a cloud pane is never re-typed" in out
        assert "Revived" not in out and sent == []

    def _up_with_a_live_cloud_pane(self, runner, monkeypatch, cfg_path, *args):
        from magent import cli

        row = {
            "name": "api",
            "session": "api",
            "path": "x",
            "tool": "claude",
            "group": None,
            "resolved": "x",
            "cmd": 'claude --cloud "t"',
            "node": "cloud",
        }
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([{"session": "api", "name": "api"}], [], [row]),
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda *a, **k: []
        )
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a, **k: [])
        return runner.invoke(cli.main, ["--config", cfg_path, "up", *args])

    def test_up_json_revive_types_nothing_into_a_live_cloud_pane(
        self, sent, runner, monkeypatch, tmp_config, tmp_path
    ):
        cfg = _cloud_cfg(tmp_config, tmp_path)
        result = self._up_with_a_live_cloud_pane(
            runner, monkeypatch, cfg, "--json", "--revive"
        )
        assert json.loads(result.stdout)["revived"] == []
        assert sent == []

    def test_interactive_up_types_nothing_into_a_live_cloud_pane(
        self, sent, runner, monkeypatch, tmp_config, tmp_path
    ):
        # The interactive path revives unconditionally (no flag to opt out of).
        cfg = _cloud_cfg(tmp_config, tmp_path, settings={"uploadServer": False})
        result = self._up_with_a_live_cloud_pane(runner, monkeypatch, cfg)
        assert result.exit_code == 0
        assert sent == [] and "Revived" not in result.output


def _shared_cwd_cfg(tmp_config, tmp_path: Path, *, same_folder=True) -> str:
    """A cloud project (session ``api``) and a local one titled ``api-local``.
    ``same_folder`` puts both in ONE directory: two live agents, one cwd."""
    cloud_dir = tmp_path / "api"
    cloud_dir.mkdir(exist_ok=True)
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    local: dict[str, object] = {
        "path": str(cloud_dir if same_folder else other),
        "title": "api-local",
    }
    cloud: dict[str, object] = {
        "path": str(cloud_dir),
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    return tmp_config({"version": SCHEMA_VERSION, "projects": [cloud, local]})


class TestTheIdleReaperNeverParksACloudPane:
    @pytest.fixture(autouse=True)
    def _every_session_is_live(self, monkeypatch):
        monkeypatch.setattr(
            psmux, "live_sessions", lambda names, psmux=None, **kw: list(names)
        )
        monkeypatch.setattr(psmux, "pane_trees", lambda names, **kw: {})

    def test_a_live_cloud_pane_is_out_of_scope(self, tmp_config, tmp_path):
        # Parking a pane is `--resume <id>` later; a cloud pane has no local
        # conversation, and its VM is not what the reaper measures.
        from magent import reap
        from magent.sessions import AGENT_TOOLS

        cloud = load_config(_cloud_cfg(tmp_config, tmp_path))
        scoped, counts = reap._scope(cloud, tools=AGENT_TOOLS, psmux_bin="psmux")
        # Never a candidate, but a live agent in its folder all the same: R3
        # counts it (see the shared-folder tests below).
        assert scoped == [] and list(counts.values()) == [1]
        # The control: the same project as a local one IS in scope (claude has
        # an idle probe).
        local = load_config(_cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None))
        scoped, _ = reap._scope(local, tools=AGENT_TOOLS, psmux_bin="psmux")
        assert [r["session"] for r in scoped] == ["api"]

    def test_the_sweep_and_the_just_before_the_stop_recheck_never_see_it(
        self, tmp_config, tmp_path
    ):
        # `gather` picks who to park and `_read_one` re-reads ONE session right
        # before the stop: both go through `_scope`.
        from magent import reap
        from magent.sessions import AGENT_TOOLS

        cfg = load_config(_cloud_cfg(tmp_config, tmp_path))
        kw = {
            "tools": AGENT_TOOLS,
            "config_dir": None,
            "now": 1.0,
            "psmux_bin": "psmux",
        }
        assert reap.gather(cfg, **kw).rows == {}
        assert reap._read_one(cfg, "api", **kw) is None


class TestACloudPaneStillCountsTowardsASharedFolder:
    """R3 spares a session that does not own its directory. A cloud pane is
    never a CANDIDATE, but its local `claude --cloud` is a live agent in that
    folder: a local session sharing it could be handed the cloud pane's process
    or session file and parked on the wrong idle signal. So the cloud pane is
    out of the candidates and IN the count."""

    @pytest.fixture(autouse=True)
    def _live(self, monkeypatch):
        self.live = None  # None = every session answers
        monkeypatch.setattr(
            psmux,
            "live_sessions",
            lambda names, psmux=None, **kw: [
                n for n in names if self.live is None or n in self.live
            ],
        )
        monkeypatch.setattr(psmux, "pane_trees", lambda names, **kw: {})

    @pytest.fixture
    def kw(self):
        from magent.sessions import AGENT_TOOLS

        return {
            "tools": AGENT_TOOLS,
            "config_dir": None,
            "now": 1.0,
            "psmux_bin": "psmux",
        }

    def test_the_cloud_pane_is_counted_but_never_a_candidate(
        self, tmp_config, tmp_path, kw
    ):
        from magent import reap

        cfg = load_config(_shared_cwd_cfg(tmp_config, tmp_path))
        scoped, counts = reap._scope(cfg, tools=kw["tools"], psmux_bin="psmux")
        assert [r["session"] for r in scoped] == ["api-local"]
        assert list(counts.values()) == [2]

    def test_the_sweep_vetoes_the_local_session_and_never_lists_the_cloud_one(
        self, tmp_config, tmp_path, kw
    ):
        from magent import reap

        cfg = load_config(_shared_cwd_cfg(tmp_config, tmp_path))
        sweep = reap.gather(cfg, **kw)
        assert list(sweep.rows) == ["api-local"]
        assert sweep.rows["api-local"].reason == "shared-cwd"

    def test_the_just_before_the_stop_recheck_vetoes_it_too(
        self, tmp_config, tmp_path, kw
    ):
        from magent import reap

        cfg = load_config(_shared_cwd_cfg(tmp_config, tmp_path))
        row = reap._read_one(cfg, "api-local", **kw)
        assert row is not None and row.reason == "shared-cwd"
        assert reap._read_one(cfg, "api", **kw) is None

    def test_a_cloud_pane_in_another_folder_vetoes_nothing(
        self, tmp_config, tmp_path, kw
    ):
        # The control: the veto is about the DIRECTORY, not about a cloud pane
        # existing somewhere.
        from magent import reap

        cfg = load_config(_shared_cwd_cfg(tmp_config, tmp_path, same_folder=False))
        sweep = reap.gather(cfg, **kw)
        assert sweep.rows["api-local"].reason != "shared-cwd"

    def test_a_cloud_pane_that_is_not_live_is_not_counted(
        self, tmp_config, tmp_path, kw
    ):
        # No live pane, no local `claude` to confuse with the local session's.
        from magent import reap

        self.live = {"api-local"}
        cfg = load_config(_shared_cwd_cfg(tmp_config, tmp_path))
        sweep = reap.gather(cfg, **kw)
        assert sweep.rows["api-local"].reason != "shared-cwd"


class TestBringUpGatesTheCloudCreate:
    def _run(
        self,
        monkeypatch,
        fake_platform,
        cfg_path,
        *,
        refusal=None,
        live=False,
        only=None,
        failed=None,
    ):
        windows: list[psmux.PsmuxWindowOpts] = []
        asked: list[str] = []
        probed: list[list[str]] = []
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(
            "magent.launch.cloud_refusal",
            lambda config, sid: (asked.append(sid), refusal)[1],
        )
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        # THE liveness answer, asked once for every cloud project.
        monkeypatch.setattr(
            psmux,
            "live_sessions",
            lambda names, psmux=None, **kw: (
                probed.append(list(names)),
                list(names) if live else [],
            )[1],
        )
        monkeypatch.setattr(
            psmux,
            "launch_verified",
            lambda plat, wins: (windows.extend(wins), dict(failed or {}))[1],
        )
        result = psmux.bring_up(load_config(cfg_path), only=only)
        return result, windows, asked, probed

    def test_a_refused_cloud_create_is_a_failure_with_its_reason(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        result, windows, _, _ = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            refusal="main is dirty",
        )
        assert (result, windows) == (([], {"api": "main is dirty"}), [])

    def test_an_accepted_cloud_create_is_typed_once_and_branded(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        (up, failed), [w], asked, _ = self._run(
            monkeypatch, fake_platform, _cloud_cfg(tmp_config, tmp_path)
        )
        assert (up, failed, asked) == (["api"], {}, ["api"])
        assert (w.command, w.resend, w.nick) == (
            'claude --cloud "Fix the login bug"',
            False,
            "cloud",
        )

    def test_a_live_cloud_session_is_left_to_launch_verified_ungated(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        (_, failed), windows, asked, _ = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            refusal="dirty",
            live=True,
        )
        assert (failed, len(windows), asked) == ({}, 1, [])

    def test_a_local_project_never_asks_the_gate_or_the_liveness_seam(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        (up, failed), [w], asked, probed = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None),
        )
        assert (up, failed, asked, probed) == (["api"], {}, [], [])
        assert (w.resend, w.nick) == (True, None)

    def test_a_project_outside_only_is_not_gated_or_probed(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        result, windows, asked, probed = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            refusal="dirty",
            only=["web"],
        )
        assert (result, windows, asked, probed) == (([], {}), [], [], [])

    def test_a_refusal_rides_beside_launch_verifieds_own_casualties(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [
                    {
                        "path": str(tmp_path / "api"),
                        "node": "cloud",
                        "cloudTask": "Fix the login bug",
                    },
                    {"path": str(tmp_path / "web")},
                ],
            }
        )
        (up, failed), windows, _, _ = self._run(
            monkeypatch,
            fake_platform,
            path,
            refusal="main is dirty",
            failed={"web": "psmux said no"},
        )
        assert up == []
        assert failed == {"web": "psmux said no", "api": "main is dirty"}
        assert [w.window_name for w in windows] == ["web"]

    def test_a_cloud_project_with_no_task_is_refused_by_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, cloudTask=None))
        up, failed = psmux.bring_up(cfg)
        assert up == [] and failed == {"api": launch.NO_CLOUD_TASK}
        assert fake_platform.psmux_launches == []

    @pytest.mark.parametrize(
        ("proj", "settings", "said"),
        [
            ({"tool": "codex"}, None, "'codex'"),
            ({}, {"tools": {"claude": 'bash -c "claude --continue"'}}, "'bash'"),
        ],
        ids=["another-tool", "wrapper"],
    )
    def test_a_cloud_project_on_the_wrong_tool_is_refused_not_typed_as_bash(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, proj, settings, said
    ):
        # The REAL gate answers: tool first, before any git is read.
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        cfg = load_config(_cloud_cfg(tmp_config, tmp_path, settings=settings, **proj))
        up, failed = psmux.bring_up(cfg)
        assert up == [] and said in failed["api"]
        assert fake_platform.psmux_launches == []

    def test_an_executable_it_cannot_type_is_refused_by_name_not_skipped(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # Tool and task pass the gate; the command still cannot be built.
        path = _cloud_cfg(
            tmp_config,
            tmp_path,
            settings={"tools": {"claude": r"C:\a&b\claude.exe"}},
        )
        (up, failed), windows, _, _ = self._run(monkeypatch, fake_platform, path)
        assert (up, windows) == ([], [])
        assert "cannot type" in failed["api"]

    def test_a_live_pane_with_no_command_is_not_called_a_failure(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        (up, failed), windows, _, _ = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path, cloudTask=None),
            live=True,
        )
        assert (up, failed, windows) == ([], {}, [])

    def test_a_refusal_reaches_the_printed_casualty_list(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # launch.bring_up_psmux merges the dict and report_bring_up_casualties
        # prints each reason: nothing else carries it.
        (_, failed), _, _, _ = self._run(
            monkeypatch,
            fake_platform,
            _cloud_cfg(tmp_config, tmp_path),
            refusal="main is dirty",
        )
        launch.report_bring_up_casualties(failed)
        out = capsys.readouterr().out
        assert "failed to come up: api" in out and "main is dirty" in out


class TestBringUpNeverCreatesACloudPaneOnAnotherProjectsGate:
    """The gate is asked by session name and reads the FIRST enabled project
    with it. ``eligible_projects`` skips an IDE project, so an IDE project
    listed ahead of a cloud one for the same folder makes the gate read the
    IDE project (not cloud: it waves it through) while the pane being created
    is the cloud one: the git and .env checks would be skipped."""

    def _run(self, monkeypatch, fake_platform, path, *, live=False):
        windows: list[psmux.PsmuxWindowOpts] = []
        asked: list[str] = []
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(
            "magent.launch.cloud_refusal",
            lambda config, sid: (asked.append(sid), None)[1],
        )
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux,
            "live_sessions",
            lambda names, psmux=None, **kw: list(names) if live else [],
        )
        monkeypatch.setattr(
            psmux, "launch_verified", lambda plat, wins: (windows.extend(wins), {})[1]
        )
        return psmux.bring_up(load_config(path)), windows, asked

    def test_the_cloud_twin_of_an_ide_project_is_refused_by_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        path = _ide_twin_cfg(tmp_config, tmp_path)
        (up, failed), windows, asked = self._run(monkeypatch, fake_platform, path)
        assert (up, windows, asked) == ([], [], [])
        assert failed == {"api": launch.twin_session_refusal("api")}

    def test_the_refusal_is_the_one_the_launch_path_prints(self):
        assert launch.twin_session_refusal("api") == (
            "another enabled project uses the session name api;"
            " rename one (set a title)"
        )

    def test_the_first_project_with_the_name_is_gated_and_created(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        path = _ide_twin_cfg(tmp_config, tmp_path, cloud_first=True)
        (up, failed), [w], asked = self._run(monkeypatch, fake_platform, path)
        assert (up, failed, asked) == (["api"], {}, ["api"])
        assert (w.resend, w.nick) == (False, "cloud")

    def test_a_live_session_is_never_refused_for_its_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # Nothing is created for a live session, so there is nothing to refuse.
        path = _ide_twin_cfg(tmp_config, tmp_path)
        (_, failed), _, asked = self._run(monkeypatch, fake_platform, path, live=True)
        assert (failed, asked) == ({}, [])

    def test_a_local_twin_listed_first_owns_the_pane_and_it_is_not_cloud(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        # `eligible_projects` keeps the first of two projects with one name:
        # the local one, so no cloud command is typed and no gate is asked.
        path = _twin_cfg(tmp_config, tmp_path)
        (up, failed), [w], asked = self._run(monkeypatch, fake_platform, path)
        assert (up, failed, asked) == (["api"], {}, [])
        assert (w.resend, w.nick) == (True, None)
        assert "--cloud" not in w.command


class TestUpReportsACloudRefusal:
    """Both bring-up surfaces print a refused create under the casualty line:
    nothing about a refusal is silent."""

    def _setup(self, monkeypatch, fake_platform, tmp_config, tmp_path):
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(
            "magent.launch.cloud_refusal", lambda config, sid: "main is dirty"
        )
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a, **k: [])
        return _cloud_cfg(tmp_config, tmp_path, settings={"uploadServer": False})

    def test_magent_up(self, runner, monkeypatch, fake_platform, tmp_config, tmp_path):
        from magent import cli

        cfg = self._setup(monkeypatch, fake_platform, tmp_config, tmp_path)
        result = runner.invoke(cli.main, ["--config", cfg, "up"])
        assert "failed to come up: api" in result.output
        assert "main is dirty" in result.output
        assert fake_platform.psmux_launches == []

    def test_the_status_menus_bring_up(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        from pathlib import Path

        import click

        from magent.cli import status as status_mod

        cfg = self._setup(monkeypatch, fake_platform, tmp_config, tmp_path)
        monkeypatch.setattr(click, "prompt", lambda *a, **k: "a")
        monkeypatch.setattr(click, "pause", lambda *a, **k: None)
        status_mod._menu_up(Path(cfg))
        out = capsys.readouterr().out
        assert "failed to come up: api" in out and "main is dirty" in out
        assert fake_platform.psmux_launches == []


class TestAnOnceOnlyPaneTheVerifyFoundLateIsNotToldToBeRevived:
    """A refused ``new-session`` the verify then finds live gets
    ``_LATE_LIVE``: "run `magent up` to revive it". For a cloud pane that is
    wrong twice: revive never re-types one (vetoed), and an `up` that finds the
    session live creates nothing. The advice that works is down, then up."""

    def _verify(self, monkeypatch, window):
        from tests.conftest import FakePlatform

        why = "psmux new-session for api gave no answer within 60s"
        fp = FakePlatform(supports_psmux=True)

        def _launch(windows):
            fp.psmux_sessions.update(w.window_name for w in windows)
            return {w.window_name: why for w in windows}

        monkeypatch.setattr(fp, "launch_psmux_session", _launch)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.psmux.time.sleep", lambda s: None)
        monkeypatch.setattr(
            psmux,
            "has_session",
            lambda name, psmux=None, timeout=None: name in fp.psmux_sessions,
        )
        return why, psmux.launch_verified(fp, [window])

    @staticmethod
    def _win(**kw):
        return psmux.PsmuxWindowOpts(
            window_name="api", cwd="/a/api", command="claude", **kw
        )

    def test_a_cloud_pane_is_told_to_take_it_down_first(self, monkeypatch):
        why, failed = self._verify(monkeypatch, self._win(resend=False, nick="cloud"))
        text = failed["api"]
        assert text.startswith(why)
        assert "revive" not in text
        # A refused window never reached send-keys, so this bring-up typed
        # nothing: say that, not "cannot tell" (that is the missing-pane case).
        assert "typed no agent command into it" in text
        assert "cannot tell" not in text
        assert "`magent down api`" in text and "`magent up`" in text
        assert "claude.ai/code" in text
        assert text.isascii()

    def test_an_ordinary_pane_keeps_the_advice_it_had(self, monkeypatch):
        why, failed = self._verify(monkeypatch, self._win())
        assert failed == {"api": why + psmux._LATE_LIVE}

    def test_the_missing_pane_advice_still_reads_right(self):
        # Missing is not live: `magent up` runs the gate and creates it, which
        # is exactly what this text asks for, after the user checked the site.
        assert "claude.ai/code" in psmux._NOT_RETYPED
        assert "`magent up`" in psmux._NOT_RETYPED
        assert "revive" not in psmux._NOT_RETYPED


class TestUpJsonCarriesTheNode:
    def test_the_projects_list_names_the_cloud_node(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        from magent import cli

        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        result = runner.invoke(
            cli.main, ["--config", _cloud_cfg(tmp_config, tmp_path), "up", "--json"]
        )
        [proj] = json.loads(result.stdout)["projects"]
        assert proj["node"] == "cloud"

    def test_a_local_project_says_none(self, runner, monkeypatch, tmp_config, tmp_path):
        from magent import cli

        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        path = _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None)
        result = runner.invoke(cli.main, ["--config", path, "up", "--json"])
        [proj] = json.loads(result.stdout)["projects"]
        assert proj["node"] is None


_ROW = {
    "name": "api",
    "session": "api",
    "path": "x",
    "tool": "claude",
    "group": None,
    "resolved": "x",
    "cmd": "c",
}


class TestUpBrandsAFreshCloudPaneAtCloud:
    """The status line is re-decorated by `up` on every live session and on
    every session just created. A decoration without the nick would overwrite
    the `@cloud` brand with the plain one."""

    def _json(self, runner, monkeypatch, cfg_path, rows):
        from magent import cli

        seen: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"session": r["session"], "name": r["name"]} for r in rows],
                [],
                rows,
            ),
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async",
            lambda names, **kw: (
                seen.append((list(names), kw.get("nicks", {}))) or list(names)
            ),
        )
        runner.invoke(cli.main, ["--config", cfg_path, "up", "--json"])
        return seen

    def _interactive(self, runner, monkeypatch, cfg_path, rows, *, up, down, created):
        from magent import cli

        seen: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status", lambda cfg, group=None: (up, down, rows)
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux", lambda *a, **k: (list(created), {})
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions",
            lambda names, **kw: (
                seen.append((list(names), kw.get("nicks", {}))) or list(names)
            ),
        )
        runner.invoke(cli.main, ["--config", cfg_path, "up"])
        return seen

    def test_a_live_cloud_session_is_redecorated_with_its_nick(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        row = {**_ROW, "node": "cloud"}
        path = _cloud_cfg(tmp_config, tmp_path)
        assert self._json(runner, monkeypatch, path, [row]) == [
            (["api"], {"api": "cloud"})
        ]

    def test_only_the_cloud_session_of_a_mixed_fleet_is_nicked(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        rows = [
            {**_ROW, "node": "cloud"},
            {**_ROW, "name": "web", "session": "web", "node": None},
        ]
        path = _cloud_cfg(tmp_config, tmp_path)
        assert self._json(runner, monkeypatch, path, rows) == [
            (["api", "web"], {"api": "cloud"})
        ]

    def test_without_a_cloud_project_the_call_is_the_positional_one_it_was(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        # The existing tests fake these wrappers as `lambda names: names`, so
        # a `nicks=` keyword must not be passed when there is nothing to brand.
        from magent import cli

        seen: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"session": "api", "name": "api"}],
                [],
                [{**_ROW, "node": None}],
            ),
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async",
            lambda names: seen.append(list(names)) or list(names),
        )
        path = _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None)
        runner.invoke(cli.main, ["--config", path, "up", "--json"])
        assert seen == [["api"]]

    def test_the_interactive_decoration_of_a_live_cloud_session_is_nicked(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        path = _cloud_cfg(tmp_config, tmp_path, settings={"uploadServer": False})
        seen = self._interactive(
            runner,
            monkeypatch,
            path,
            [{**_ROW, "node": "cloud"}],
            up=[{"session": "api", "name": "api"}],
            down=[],
            created=[],
        )
        assert seen == [(["api"], {"api": "cloud"})]

    def test_the_interactive_decoration_of_a_session_just_created_is_nicked(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        path = _cloud_cfg(tmp_config, tmp_path, settings={"uploadServer": False})
        seen = self._interactive(
            runner,
            monkeypatch,
            path,
            [{**_ROW, "node": "cloud"}],
            up=[],
            down=[{"session": "api", "name": "api"}],
            created=["api"],
        )
        assert seen == [(["api"], {"api": "cloud"})]

    def test_the_interactive_call_without_a_cloud_project_is_positional(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        from magent import cli

        seen: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"session": "api", "name": "api"}],
                [],
                [{**_ROW, "node": None}],
            ),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions",
            lambda names: seen.append(list(names)) or list(names),
        )
        path = _cloud_cfg(
            tmp_config,
            tmp_path,
            node=None,
            cloudTask=None,
            settings={"uploadServer": False},
        )
        runner.invoke(cli.main, ["--config", path, "up"])
        assert seen == [["api"]]


# ---------------------------------------------------------------------------
# J8 (follow-up): a LOCAL project that shares a cloud project's session name.
# Its window would be created, verified and re-sent into the live cloud pane
# (`claude --continue` typed into a `claude --cloud` session), and a later
# revive could do the same.
# ---------------------------------------------------------------------------


def _grouped_twin_cfg(tmp_config, tmp_path: Path, *, cloud_first=True) -> str:
    """A cloud project (group a) and a local one (group b) for the SAME folder:
    one session name, two groups, so a group filter drops one of them before
    the first-wins dedupe could."""
    folder = tmp_path / "api"
    folder.mkdir(exist_ok=True)
    local: dict[str, object] = {"path": str(folder), "group": "b"}
    cloud: dict[str, object] = {
        "path": str(folder),
        "group": "a",
        "node": "cloud",
        "cloudTask": "Fix the login bug",
    }
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {"psmux": True},
            "projects": [cloud, local] if cloud_first else [local, cloud],
        }
    )


class TestTheLaunchPathNeverQueuesALocalWindowOnACloudPanesName:
    @pytest.mark.parametrize(
        ("running", "live"), [(False, False), (False, True), (True, False)]
    )
    def test_the_local_twin_of_an_earlier_cloud_project_is_skipped_by_name(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys, running, live
    ):
        # Cloud first: the cloud entry is `project_for_session`'s first enabled
        # match, so it passes its own twin check. The local entry must not then
        # queue a `resend=True` window under the same name: that is the window
        # whose verify re-sends `claude --continue` into the cloud pane.
        path = _twin_cfg(tmp_config, tmp_path, cloud_first=True)
        n, windows, asked, targets, _ = _dispatch(
            monkeypatch, fake_platform, path, which=1, running=running, live=live
        )
        out = capsys.readouterr().out
        assert (n, windows, asked, targets) == (0, [], [], [])
        assert out.count("SKIP:") == 1
        assert launch.twin_session_refusal("api") in out

    def test_the_loop_creates_the_one_cloud_window_and_skips_the_local_one(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        monkeypatch.setattr("magent.launch.cloud_refusal", lambda config, sid: None)
        monkeypatch.setattr("magent.psmux.live_sessions", lambda names, *a, **kw: [])
        fake_platform._supports_psmux = True
        cfg = load_config(_twin_cfg(tmp_config, tmp_path, cloud_first=True))
        result = launch._launch_projects(
            fake_platform, cfg, RunOpts(), cfg.projects, None
        )
        out = capsys.readouterr().out
        [w] = result.psmux_windows
        assert (w.command, w.resend, w.nick) == (
            'claude --cloud "Fix the login bug"',
            False,
            "cloud",
        )
        assert out.count("SKIP:") == 1 and launch.twin_session_refusal("api") in out
        assert [t.key for t in result.targets] == ["api"]

    def test_a_disabled_cloud_project_owns_nothing(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # `project_for_session` reads ENABLED projects only: a disabled cloud
        # entry creates no pane, so it must not take the local project's name.
        folder = tmp_path / "api"
        folder.mkdir()
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"psmux": True},
                "projects": [
                    {
                        "path": str(folder),
                        "node": "cloud",
                        "cloudTask": "t",
                        "enabled": False,
                    },
                    {"path": str(folder)},
                ],
            }
        )
        n, [w], _, _, _ = _dispatch(monkeypatch, fake_platform, path, which=1)
        assert (n, w.resend, w.nick) == (1, True, None)
        assert "SKIP" not in capsys.readouterr().out

    def test_the_other_order_is_still_the_cloud_twins_skip(
        self, monkeypatch, fake_platform, tmp_config, tmp_path, capsys
    ):
        # [local, cloud]: the J7 behaviour, unchanged -- the local project
        # keeps its window, the cloud entry is the one skipped.
        path = _twin_cfg(tmp_config, tmp_path, cloud_first=False)
        n, [w], _, _, _ = _dispatch(monkeypatch, fake_platform, path, which=0)
        assert (n, w.resend, w.nick) == (1, True, None)
        n, windows, asked, _, _ = _dispatch(monkeypatch, fake_platform, path, which=1)
        assert (n, windows, asked) == (0, [], [])
        assert launch.twin_session_refusal("api") in capsys.readouterr().out


class TestACloudFirstTwinNeverReachesTheSessionLists:
    """``up``, revive, status and the reaper read ``eligible_projects``. Without
    a group filter its first-wins dedupe already keeps the local twin out; with
    one, the cloud project can be filtered away FIRST, and the local twin would
    become the row that owns the cloud pane's name."""

    @pytest.fixture
    def sent(self, monkeypatch):
        out: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, **k: list(names))
        monkeypatch.setattr(psmux, "idle_sessions", lambda names, **k: set(names))
        monkeypatch.setattr(
            psmux, "send_keys", lambda sid, *a, **k: out.append(sid) or True
        )
        return out

    def test_without_a_group_the_cloud_project_owns_the_name(
        self, tmp_config, tmp_path
    ):
        cfg = load_config(_twin_cfg(tmp_config, tmp_path, cloud_first=True))
        [entry] = psmux.eligible_projects(cfg)
        assert entry["node"] == "cloud" and "--cloud" in str(entry["cmd"])

    def test_a_group_filter_leaves_the_local_twin_no_command_and_a_reason(
        self, tmp_config, tmp_path
    ):
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path))
        [entry] = psmux.eligible_projects(cfg, "b")
        assert entry["node"] is None and entry["cmd"] == ""
        assert entry["cmd_why"] == launch.twin_session_refusal("api")

    def test_the_other_order_leaves_the_local_project_alone(self, tmp_config, tmp_path):
        # [local, cloud]: the local project owns the name, so its row is the
        # ordinary one in either group view.
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path, cloud_first=False))
        [entry] = psmux.eligible_projects(cfg, "b")
        assert entry["cmd"] and "cmd_why" not in entry

    def test_a_project_with_no_cloud_twin_is_untouched(self, tmp_config, tmp_path):
        cfg = load_config(
            _cloud_cfg(tmp_config, tmp_path, node=None, cloudTask=None, group="b")
        )
        [entry] = psmux.eligible_projects(cfg, "b")
        assert entry["cmd"] and "cmd_why" not in entry

    def test_status_says_why_the_local_twin_is_down(
        self, monkeypatch, tmp_config, tmp_path
    ):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path))
        _up, down, _all = psmux.psmux_status(cfg, "b")
        assert [d["reason"] for d in down] == [launch.twin_session_refusal("api")]

    def test_bring_up_refuses_the_local_twin_by_name_and_creates_nothing(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        windows: list[psmux.PsmuxWindowOpts] = []
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        monkeypatch.setattr(
            psmux, "launch_verified", lambda plat, wins: (windows.extend(wins), {})[1]
        )
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path))
        up, failed = psmux.bring_up(cfg, group="b")
        assert (up, windows) == ([], [])
        assert failed == {"api": launch.twin_session_refusal("api")}

    def test_bring_up_without_a_group_makes_one_window_and_it_is_the_cloud_one(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        windows: list[psmux.PsmuxWindowOpts] = []
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr("magent.launch.cloud_refusal", lambda config, sid: None)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(psmux, "live_sessions", lambda names, psmux=None, **kw: [])
        monkeypatch.setattr(
            psmux, "launch_verified", lambda plat, wins: (windows.extend(wins), {})[1]
        )
        cfg = load_config(_twin_cfg(tmp_config, tmp_path, cloud_first=True))
        up, failed = psmux.bring_up(cfg)
        [w] = windows
        assert (up, failed) == (["api"], {})
        assert (w.resend, w.nick) == (False, "cloud")

    def test_revive_never_types_into_the_cloud_panes_name_for_the_local_twin(
        self, sent, tmp_config, tmp_path
    ):
        why: dict[str, str] = {}
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path))
        revived = psmux.revive_sessions(cfg, only=["api"], group="b", vetoed=why)
        assert revived == [] and sent == []
        assert why["api"] == launch.twin_session_refusal("api")


# ---------------------------------------------------------------------------
# J9: the user-facing surfaces tell a cloud pane apart from an agent pane.
# Every `claude --cloud` typed into a pane starts a NEW billed cloud session
# the CLI cannot list or stop, so no surface may type into a cloud pane, and
# each must tell the truth about one.
# ---------------------------------------------------------------------------


def _hermetic_fleet(monkeypatch, *, live: bool = True) -> list[str]:
    """Every pane-driving seam `send`/`model`/`peek` reach, faked: no test here
    may touch a real psmux (the box that runs them hosts a live fleet). The
    returned list records each pane a command WROTE to -- empty means nothing
    was typed."""
    typed: list[str] = []
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
    monkeypatch.setattr(
        "magent.psmux.live_sessions",
        lambda names, psmux=None: list(names) if live else [],
    )
    monkeypatch.setattr(
        "magent.fleet.paste_and_enter",
        lambda name, text, psmux_bin=None: typed.append(name) or True,
    )
    monkeypatch.setattr(
        "magent.fleet.switch_model",
        lambda name, model, effort, psmux_bin=None: typed.append(name) or True,
    )
    monkeypatch.setattr(
        "magent.fleet.read_state",
        lambda name, psmux_bin=None: {"state": "idle", "model": "m", "effort": "e"},
    )
    monkeypatch.setattr(
        "magent.psmux.read_pane",
        lambda name, psmux=None: SimpleNamespace(timed_out=False, text=""),
    )
    monkeypatch.setattr("magent.psmux.capture_pane", lambda name, psmux=None: "")
    monkeypatch.setattr("magent.cli.fleet_cmd.time.sleep", lambda s: None)
    return typed


class TestTheSurfacesTellCloudApart:
    def test_sessions_json_carries_the_node(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        result = runner.invoke(
            cli.main,
            ["--config", _cloud_cfg(tmp_config, tmp_path), "sessions", "--json"],
        )
        [row] = json.loads(result.stdout)
        assert row["node"] == "cloud"

    def test_a_live_cloud_row_is_never_read_as_an_idle_agent(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        _hermetic_fleet(monkeypatch)
        reads: list[str] = []
        monkeypatch.setattr(
            "magent.fleet.read_state",
            lambda name, psmux_bin=None: (
                reads.append(name) or {"state": "idle", "model": "m", "effort": "e"}
            ),
        )
        result = runner.invoke(
            cli.main,
            ["--config", _cloud_cfg(tmp_config, tmp_path), "sessions", "--json"],
        )
        [row] = json.loads(result.stdout)
        # A cloud pane at a bare shell is quiet, and a quiet pane classifies as
        # "idle": the pane is not read at all, and the row says what it is.
        assert (row["live"], row["state"], row["node"]) == (True, "cloud", "cloud")
        assert (row["model"], row["effort"]) == (None, None)
        assert reads == []

    def test_send_refuses_a_cloud_session_and_names_the_cli_follow_up(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch)
        result = runner.invoke(
            cli.main,
            ["--config", _cloud_cfg(tmp_config, tmp_path), "send", "api", "hello"],
        )
        assert result.exit_code == 2
        assert "cloud session" in result.stderr
        # V5: valid ONLY as a follow-up to an existing session id -- and the id
        # is the user's: nothing here claims to know it, or where it is listed.
        assert 'claude -p "<msg>" --cloud <session-id>' in result.stderr
        assert "continues an existing cloud session" in result.stderr
        assert "magent does not track it" in result.stderr
        assert "claude.ai/code" not in result.stderr
        assert "lists" not in result.stderr
        assert typed == []

    def test_model_refuses_a_cloud_session(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch)
        result = runner.invoke(
            cli.main,
            ["--config", _cloud_cfg(tmp_config, tmp_path), "model", "api", "opus"],
        )
        assert result.exit_code == 2
        assert "cloud session" in result.stderr
        assert typed == []

    def test_model_all_and_send_never_see_a_cloud_pane_as_live(
        self, monkeypatch, tmp_config, tmp_path
    ):
        from magent.cli import fleet_cmd

        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        cfg = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [
                    {"path": str(tmp_path / "api"), "node": "cloud", "cloudTask": "t"},
                    {"path": str(tmp_path / "web")},
                ],
            }
        )
        _hermetic_fleet(monkeypatch)
        assert fleet_cmd._live_names(cfg, "psmux") == ["web"]
        # peek is read-only, so it keeps the cloud pane in view.
        assert fleet_cmd._live_names(cfg, "psmux", drivable=False) == ["api", "web"]

    def test_peek_still_reads_a_cloud_pane(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        _hermetic_fleet(monkeypatch)
        monkeypatch.setattr(
            "magent.psmux.read_pane",
            lambda name, psmux=None: SimpleNamespace(
                timed_out=False, text="provisioning"
            ),
        )
        result = runner.invoke(
            cli.main, ["--config", _cloud_cfg(tmp_config, tmp_path), "peek", "api"]
        )
        assert result.exit_code == 0
        assert "provisioning" in result.stdout

    def test_down_says_the_cloud_session_keeps_running(self, capsys):
        from magent.cli.status import _report_shutdown

        _report_shutdown(["api", "web"], [], cloud={"api"})
        out = capsys.readouterr().out
        assert "api was a cloud pane" in out
        assert "keeps running" in out
        assert "claude.ai/code" in out
        assert "web was a cloud pane" not in out

    def test_cloud_is_the_fifth_parameter_and_displaces_neither_node_half(self, capsys):
        from magent.cli.status import _report_shutdown

        _report_shutdown([], [], [], ["web"], cloud={"api"})
        out = capsys.readouterr().out
        assert "would NOT stop: web" in out and "nodes.log" in out
        # api was not stopped here, so nothing is claimed about its cloud session.
        assert "api was a cloud pane" not in out

    def _stopped_api(self, monkeypatch):
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"session": "api", "name": "api"}],
                [],
                [{"session": "api", "name": "api"}],
            ),
        )
        monkeypatch.setattr(
            "magent.launch.stop_psmux", lambda targets: (list(targets), [])
        )

    def test_down_passes_the_cloud_panes_to_the_report(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        """`down` and the menu's shut-down both hand the report the cloud set;
        a call site that forgot it would say nothing about the cloud session."""
        self._stopped_api(monkeypatch)
        result = runner.invoke(
            cli.main, ["--config", _cloud_cfg(tmp_config, tmp_path), "down", "api"]
        )
        assert "api was a cloud pane" in result.output

    def test_the_menus_shut_down_passes_them_too(
        self, monkeypatch, capsys, tmp_config, tmp_path
    ):
        from pathlib import Path

        from magent.cli import status as status_mod

        self._stopped_api(monkeypatch)
        monkeypatch.setattr(status_mod, "_probe_port", lambda port: False)
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: "a")
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        status_mod._menu_down(Path(_cloud_cfg(tmp_config, tmp_path)))
        assert "api was a cloud pane" in capsys.readouterr().out

    def test_the_menus_list_flags_a_cloud_pane_before_the_confirm(
        self, monkeypatch, capsys, tmp_config, tmp_path
    ):
        """The user decides on THIS list: a cloud pane's shutdown leaves its
        cloud session running, so the list says so before anything is asked."""
        from pathlib import Path

        from magent.cli import status as status_mod

        self._stopped_api(monkeypatch)
        stopped: list[object] = []
        monkeypatch.setattr(
            "magent.launch.stop_psmux", lambda targets: (stopped.append(targets), [])
        )
        monkeypatch.setattr(status_mod, "_probe_port", lambda port: False)
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: "n")
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        status_mod._menu_down(Path(_cloud_cfg(tmp_config, tmp_path)))
        out = capsys.readouterr().out
        assert "api (cloud pane: its cloud session keeps running)" in out
        assert stopped == []

    def test_the_menus_list_leaves_a_local_pane_unannotated(
        self, monkeypatch, capsys, tmp_config, tmp_path
    ):
        from pathlib import Path

        from magent.cli import status as status_mod

        (tmp_path / "web").mkdir()
        cfg = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": str(tmp_path / "web")}]}
        )
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"session": "web", "name": "web"}],
                [],
                [{"session": "web", "name": "web"}],
            ),
        )
        monkeypatch.setattr(status_mod, "_probe_port", lambda port: False)
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: "n")
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        status_mod._menu_down(Path(cfg))
        assert "cloud" not in capsys.readouterr().out

    def test_cloud_panes_unions_the_config_set_with_the_live_twin_half(
        self, tmp_config, tmp_path
    ):
        """`_cloud_panes` is the set `down` and the menu report from: the
        config's cloud ids AND every live entry `psmux_status` marked cloud --
        a local twin standing on a live cloud pane's name (a group filter hid
        the cloud project) is in no config row, only in `up`."""
        from pathlib import Path

        from magent.cli import status as status_mod

        (tmp_path / "web").mkdir()
        plain = Path(
            tmp_config(
                {
                    "version": SCHEMA_VERSION,
                    "projects": [{"path": str(tmp_path / "web")}],
                }
            )
        )
        up = [
            {"session": "docs", "name": "docs", "node": "cloud"},
            {"session": "web", "name": "web"},
        ]
        # The up-entry half alone: no config row says `docs` is cloud.
        assert status_mod._cloud_panes(plain, up) == {"docs"}
        # The config half alone, and both together.
        cloud_cfg = Path(_cloud_cfg(tmp_config, tmp_path))
        assert status_mod._cloud_panes(cloud_cfg, []) == {"api"}
        assert status_mod._cloud_panes(cloud_cfg, up) == {"api", "docs"}


class TestModelAllLeavesACloudPaneAloneAndSaysSo:
    """`model --all` drives every live pane, so it is the one fleet command a
    cloud pane could be reached through without ever being named."""

    def _cfg(self, tmp_config, tmp_path, *, local: bool = True) -> str:
        (tmp_path / "api").mkdir()
        projects: list[dict[str, object]] = [
            {"path": str(tmp_path / "api"), "node": "cloud", "cloudTask": "t"}
        ]
        if local:
            (tmp_path / "web").mkdir()
            projects.append({"path": str(tmp_path / "web")})
        return tmp_config({"version": SCHEMA_VERSION, "projects": projects})

    def test_a_live_cloud_pane_is_never_driven_and_the_local_one_is(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch)
        result = runner.invoke(
            cli.main,
            ["--config", self._cfg(tmp_config, tmp_path), "model", "--all", "opus"],
        )
        assert "api" not in typed
        assert "web" in typed
        assert "skipped 1 cloud pane(s)" in result.stdout
        assert "api" in result.stdout.split("skipped", 1)[1].splitlines()[0]

    def test_only_cloud_panes_live_says_so_instead_of_run_up_first(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch)
        result = runner.invoke(
            cli.main,
            [
                "--config",
                self._cfg(tmp_config, tmp_path, local=False),
                "model",
                "--all",
                "opus",
            ],
        )
        assert result.exit_code == 0
        assert typed == []
        assert "skipped 1 cloud pane(s)" in result.stdout
        assert "only live panes are cloud panes" in result.stdout
        assert "No live sessions" not in result.stdout
        assert "magent up" not in result.stdout

    def test_nothing_live_still_says_run_up_first(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch, live=False)
        result = runner.invoke(
            cli.main,
            ["--config", self._cfg(tmp_config, tmp_path), "model", "--all", "opus"],
        )
        assert typed == []
        assert "No live sessions" in result.stdout
        assert "skipped" not in result.stdout

    def test_a_cloud_pane_that_is_not_live_is_not_reported_skipped(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = _hermetic_fleet(monkeypatch)
        monkeypatch.setattr(
            "magent.psmux.live_sessions",
            lambda names, psmux=None: [n for n in names if n != "api"],
        )
        result = runner.invoke(
            cli.main,
            ["--config", self._cfg(tmp_config, tmp_path), "model", "--all", "opus"],
        )
        assert "web" in typed and "api" not in typed
        assert "skipped" not in result.stdout


class TestAFleetNameIsJudgedAmongEverythingTheUserCouldMean:
    """``send``/``model`` must never type into a cloud pane -- and must never
    quietly pick a LOCAL pane because the cloud one was filtered out before the
    name was read. A name is resolved among every session the user can see; a
    cloud winner is refused; an ambiguous one is still ambiguous."""

    def _cfg(self, tmp_config, tmp_path, *titles: str, cloud: str) -> str:
        projects: list[dict[str, object]] = []
        for title in titles:
            folder = tmp_path / title
            folder.mkdir()
            entry: dict[str, object] = {"path": str(folder), "title": title}
            if title == cloud:
                entry.update(node="cloud", cloudTask="t")
            projects.append(entry)
        return tmp_config({"version": SCHEMA_VERSION, "projects": projects})

    def _typed(self, monkeypatch) -> list[str]:
        return _hermetic_fleet(monkeypatch)

    def test_a_prefix_a_cloud_pane_shares_is_still_ambiguous_not_local(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = self._typed(monkeypatch)
        cfg = self._cfg(tmp_config, tmp_path, "api-cloud", "api-web", cloud="api-cloud")
        result = runner.invoke(cli.main, ["--config", cfg, "send", "api", "hello"])
        assert result.exit_code == 2
        assert typed == []

    def test_an_exact_local_name_is_not_a_cloud_refusal(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        # `api` is a substring of the cloud pane's name, but it IS a local one.
        typed = self._typed(monkeypatch)
        cfg = self._cfg(tmp_config, tmp_path, "api-cloud", "api", cloud="api-cloud")
        result = runner.invoke(cli.main, ["--config", cfg, "send", "api", "hello"])
        assert result.exit_code == 0, result.stderr
        assert typed == ["api"]

    def test_a_unique_cloud_substring_is_refused_even_when_it_is_not_live(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = self._typed(monkeypatch)
        monkeypatch.setattr("magent.psmux.live_sessions", lambda names, psmux=None: [])
        cfg = self._cfg(tmp_config, tmp_path, "api-cloud", "web", cloud="api-cloud")
        result = runner.invoke(cli.main, ["--config", cfg, "send", "cloud", "hello"])
        assert result.exit_code == 2
        assert "api-cloud is a cloud session" in result.stderr
        assert "no live session matches" not in result.stderr
        assert typed == []

    def test_model_refuses_a_unique_cloud_substring_too(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        typed = self._typed(monkeypatch)
        cfg = self._cfg(tmp_config, tmp_path, "api-cloud", "web", cloud="api-cloud")
        result = runner.invoke(cli.main, ["--config", cfg, "model", "cloud", "opus"])
        assert result.exit_code == 2
        assert "cloud session" in result.stderr
        assert typed == []

    def test_the_shadowed_cloud_project_does_not_turn_the_local_pane_cloud(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        # [local, cloud]: the local project owns the name `api`, so the pane IS
        # a local agent and `send` reaches it; `sessions --json` agrees.
        typed = self._typed(monkeypatch)
        cfg = _twin_cfg(tmp_config, tmp_path, cloud_first=False)
        sent = runner.invoke(cli.main, ["--config", cfg, "send", "api", "hello"])
        assert sent.exit_code == 0, sent.stderr
        assert typed == ["api"]
        listed = runner.invoke(cli.main, ["--config", cfg, "sessions", "--json"])
        assert {row["node"] for row in json.loads(listed.stdout)} == {None}


class TestAttachNoMuxNeverTypesACloudCommand:
    """``--no-mux`` runs ``cd <dir> && <cmd>`` straight over ssh: for a cloud
    project that is a new billed cloud session per window, on every attach."""

    def _spawned(self, monkeypatch) -> list[list[str]]:
        from magent.cli import attach

        spawned: list[list[str]] = []
        monkeypatch.setattr(
            attach.subprocess, "Popen", lambda argv, *a, **k: spawned.append(argv)
        )
        monkeypatch.setattr(attach, "_already_open", lambda sids: set())
        monkeypatch.setattr(attach, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach, "_reclaim_geometry", lambda titles: None)
        monkeypatch.setattr(attach.time, "sleep", lambda s: None)
        return spawned

    def test_attach_no_mux_never_starts_a_cloud_session_over_ssh(
        self, monkeypatch, capsys
    ):
        from magent.cli import attach

        spawned = self._spawned(monkeypatch)
        status = {
            "projects": [
                {
                    "name": "api",
                    "session": "api",
                    "node": "cloud",
                    "cmd": 'claude --cloud "t"',
                    "resolved": "/a",
                },
            ]
        }
        with pytest.raises(SystemExit):
            attach._attach_nomux("host", status)
        assert spawned == []
        assert "cloud" in capsys.readouterr().out

    def test_the_refusal_names_the_project_and_what_to_do_instead(
        self, monkeypatch, capsys
    ):
        from magent.cli import attach

        self._spawned(monkeypatch)
        status = {
            "projects": [
                {
                    "name": "api",
                    "session": "api",
                    "node": "cloud",
                    "cmd": 'claude --cloud "t"',
                    "resolved": "/a",
                },
            ]
        }
        with pytest.raises(SystemExit) as stop:
            attach._attach_nomux("host", status)
        out = capsys.readouterr().out
        assert stop.value.code == 1
        assert "api" in out
        assert "a cloud pane is not attachable with --no-mux" in out
        assert "would start a new cloud session" in out
        assert "attach without --no-mux" in out and "claude.ai/code" in out

    def test_the_other_projects_of_the_host_still_open(self, monkeypatch, capsys):
        from magent.cli import attach

        spawned = self._spawned(monkeypatch)
        status = {
            "projects": [
                {
                    "name": "api",
                    "session": "api",
                    "node": "cloud",
                    "cmd": 'claude --cloud "t"',
                    "resolved": "/a",
                },
                {
                    "name": "web",
                    "session": "web",
                    "node": None,
                    "cmd": "claude --continue",
                    "resolved": "/w",
                },
            ]
        }
        attach._attach_nomux("host", status)
        [argv] = spawned
        assert argv[-1] == "cd /w && claude --continue"
        assert "api" in capsys.readouterr().out

    def test_the_row_decides_not_the_command_text(self, monkeypatch):
        # A cloud row whose `cmd` reads like any local command is refused all
        # the same: the guard is the row's node, never a parse of the string.
        from magent.cli import attach

        spawned = self._spawned(monkeypatch)
        status = {
            "projects": [
                {
                    "name": "api",
                    "session": "api",
                    "node": "cloud",
                    "cmd": "claude",
                    "resolved": "/a",
                },
            ]
        }
        with pytest.raises(SystemExit):
            attach._attach_nomux("host", status)
        assert spawned == []


class TestALiveCloudPaneIsNeverReadAsDown:
    """``psmux_status`` used to leave a cloud pane with no command (no task, a
    tool that is not claude, a local twin's name) unprobed, so a LIVE pane read
    as "down" -- and `up` would then offer to bring it up. A reason words why
    nothing would be STARTED; it does not say the pane is not there."""

    def _status(self, monkeypatch, path, live, group=None):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        probed: list[list[str]] = []

        def _live(names, psmux=None, **kw):
            probed.append(list(names))
            return [n for n in names if n in live]

        monkeypatch.setattr(psmux, "live_sessions", _live)
        up, down, rows = psmux.psmux_status(load_config(path), group)
        return up, down, rows, probed

    def test_a_live_pane_whose_task_was_removed_is_up_and_a_cloud_pane(
        self, monkeypatch, tmp_config, tmp_path
    ):
        path = _cloud_cfg(tmp_config, tmp_path, cloudTask=None)
        up, down, _rows, _ = self._status(monkeypatch, path, {"api"})
        assert [u["session"] for u in up] == ["api"]
        assert up[0]["node"] == "cloud"
        assert down == []

    def test_a_live_pane_on_another_tool_is_up(self, monkeypatch, tmp_config, tmp_path):
        path = _cloud_cfg(tmp_config, tmp_path, tool="codex")
        up, down, _rows, _ = self._status(monkeypatch, path, {"api"})
        assert [u["session"] for u in up] == ["api"] and down == []

    def test_a_pane_that_is_not_there_keeps_the_reason_and_the_cloud_mark(
        self, monkeypatch, tmp_config, tmp_path
    ):
        path = _cloud_cfg(tmp_config, tmp_path, cloudTask=None)
        up, down, _rows, _ = self._status(monkeypatch, path, set())
        assert up == []
        assert [(d["reason"], d["node"]) for d in down] == [
            (launch.NO_CLOUD_TASK, "cloud")
        ]

    def test_a_missing_folder_is_never_probed(self, monkeypatch, tmp_config, tmp_path):
        path = _cloud_cfg(
            tmp_config, tmp_path, cloudTask=None, path=str(tmp_path / "gone")
        )
        up, down, _rows, probed = self._status(monkeypatch, path, {"api"})
        assert up == [] and [d["reason"] for d in down] == ["folder not found"]
        assert all(names == [] for names in probed)

    def test_a_local_project_with_no_command_is_still_never_probed(
        self, monkeypatch, tmp_config, tmp_path
    ):
        path = _cloud_cfg(
            tmp_config, tmp_path, node=None, cloudTask=None, tool="nosuch"
        )
        up, down, _rows, probed = self._status(monkeypatch, path, {"api"})
        assert up == [] and [d["reason"] for d in down] == ["no agent command"]
        assert all(names == [] for names in probed)
        assert "node" not in down[0]

    def test_a_local_twin_on_a_live_cloud_pane_is_that_pane_not_a_down_project(
        self, monkeypatch, tmp_config, tmp_path
    ):
        # `up -g b`: the cloud project (group a) is filtered out, its pane is
        # live, and the local twin's row is what `psmux_status` sees.
        path = _grouped_twin_cfg(tmp_config, tmp_path)
        up, down, _rows, _ = self._status(monkeypatch, path, {"api"}, group="b")
        assert down == []
        [entry] = up
        assert entry["session"] == "api" and entry["node"] == "cloud"
        # The live pane is NAMED as the owner, with the cloud project's folder.
        note = str(entry["note"])
        assert "live cloud pane" in note and str(tmp_path / "api") in note

    def test_a_local_twin_whose_cloud_pane_is_not_live_stays_a_refusal(
        self, monkeypatch, tmp_config, tmp_path
    ):
        path = _grouped_twin_cfg(tmp_config, tmp_path)
        up, down, _rows, _ = self._status(monkeypatch, path, set(), group="b")
        assert up == []
        assert [d["reason"] for d in down] == [launch.twin_session_refusal("api")]
        assert "node" not in down[0]

    def test_bring_up_does_not_count_a_twin_of_a_live_cloud_pane_as_a_casualty(
        self, monkeypatch, fake_platform, tmp_config, tmp_path
    ):
        windows: list[psmux.PsmuxWindowOpts] = []
        monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "live_sessions", lambda names, psmux=None, **kw: list(names)
        )
        monkeypatch.setattr(
            psmux, "launch_verified", lambda plat, wins: (windows.extend(wins), {})[1]
        )
        cfg = load_config(_grouped_twin_cfg(tmp_config, tmp_path))
        up, failed = psmux.bring_up(cfg, group="b")
        # Nothing was created, and nothing FAILED: the session answers, and it
        # is the cloud pane. (`status` says so; this is not a casualty.)
        assert (up, failed, windows) == ([], {}, [])


class TestUpNamesTheLiveCloudPaneThatOwnsATwinsName:
    """`up -g b` while the cloud pane (group a) is live: the pane is up, it is
    named, its `@cloud` brand survives the re-decoration, and no revive types
    into it."""

    def test_the_interactive_up_says_who_owns_the_name_and_keeps_the_brand(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        decorated: list[tuple[list[str], dict[str, str]]] = []
        revive_calls: list[object] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "live_sessions", lambda names, psmux=None, **kw: list(names)
        )
        monkeypatch.setattr(
            "magent.launch.revive_psmux",
            lambda *a, **k: revive_calls.append(a) or [],
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions",
            lambda names, **kw: (
                decorated.append((list(names), kw.get("nicks", {}))) or list(names)
            ),
        )
        monkeypatch.setattr(
            "magent.cli.attach._maybe_start_upload_server", lambda *a, **k: None
        )
        path = _grouped_twin_cfg(tmp_config, tmp_path)
        result = runner.invoke(cli.main, ["--config", path, "up", "-g", "b"])
        assert result.exit_code == 0, result.output
        assert "live cloud pane" in result.output
        assert str(tmp_path / "api") in result.output
        assert "failed to come up" not in result.output
        assert decorated == [(["api"], {"api": "cloud"})]

    def test_the_json_up_entry_is_a_cloud_one(
        self, runner, monkeypatch, tmp_config, tmp_path
    ):
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "live_sessions", lambda names, psmux=None, **kw: list(names)
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async",
            lambda names, **kw: list(names),
        )
        path = _grouped_twin_cfg(tmp_config, tmp_path)
        result = runner.invoke(cli.main, ["--config", path, "up", "--json", "-g", "b"])
        [entry] = json.loads(result.stdout)["up"]
        assert entry["session"] == "api" and entry["node"] == "cloud"
        assert json.loads(result.stdout)["down"] == []


class TestASessionListNamesACloudPaneByItsFirstOwner:
    def test_the_first_row_for_a_session_name_decides(self):
        rows = [
            {"session": "api", "node": None},
            {"session": "api", "node": "cloud"},
            {"session": "web", "node": "cloud"},
            {"session": "web", "node": None},
            {"name": "docs", "node": "cloud"},
        ]
        assert psmux.cloud_pane_ids(rows) == {"web", "docs"}

    def test_a_row_with_no_name_is_ignored(self):
        assert psmux.cloud_pane_ids([{"node": "cloud"}]) == set()
