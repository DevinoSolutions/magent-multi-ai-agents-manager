"""Remote config editing: `config cat`/`config put` (the host half) and
`config edit` (the client half that drives them over SSH).

No test here goes near a real ssh binary, a real host, or the developer's own
config: the client tests monkeypatch `attach._ssh_capture` and the blocking
editor, the host tests drive a `--config <tmp_path>` file. The last-attach-host
memory lives in the real `~/.magent`, so `_read_last_host` is monkeypatched
anywhere the fallback is exercised -- never read from disk.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from magent import cli
from magent.cli import attach, config_editor

if TYPE_CHECKING:
    from pathlib import Path

# Deliberately un-canonical: 4-space indent, an unknown top-level key and an
# unknown project key. `cat` must hand back these exact bytes, and a `put`
# round trip must not drop the unmodeled keys.
RAW_CONFIG = """{
    "version": 3,
    "baseDir": "C:/projects",
    "layout": {"columns": 3, "rows": 2},
    "settings": {"defaultTool": "claude", "tools": {"claude": "claude --continue"}},
    "projects": [
        {"path": "api", "group": "work", "somethingMagentNeverHeardOf": 42}
    ],
    "aFutureTopLevelKey": {"nested": true}
}
"""


@pytest.fixture
def host_config(tmp_path):
    """A config file standing in for the HOST's own config."""
    p = tmp_path / "magent.config.json"
    p.write_text(RAW_CONFIG, encoding="utf-8")
    return p


def _invoke(runner, config_file, *args, **kwargs):
    return runner.invoke(
        cli.main, ["--config", str(config_file), "config", *args], **kwargs
    )


class TestConfigCat:
    """The host half's reader: a byte pipe, not a report."""

    def test_prints_the_exact_bytes_including_unknown_keys(self, runner, host_config):
        result = _invoke(runner, host_config, "cat")
        assert result.exit_code == 0
        assert result.stdout == RAW_CONFIG
        assert "aFutureTopLevelKey" in result.stdout
        assert "somethingMagentNeverHeardOf" in result.stdout

    def test_stdout_is_parseable_json(self, runner, host_config):
        result = _invoke(runner, host_config, "cat")
        assert json.loads(result.stdout)["baseDir"] == "C:/projects"

    def test_missing_config_exits_nonzero_with_a_message(self, runner, tmp_path):
        result = _invoke(runner, tmp_path / "nope.json", "cat")
        assert result.exit_code == 1
        assert "cannot read" in result.output


class TestConfigPut:
    """The host half's writer: validate, back up, then swap in atomically."""

    def _files(self, config_file: Path) -> set[str]:
        return {p.name for p in config_file.parent.iterdir()}

    def test_rejects_invalid_json_without_touching_disk(self, runner, host_config):
        result = _invoke(runner, host_config, "put", input="{not json at all")
        assert result.exit_code == 1
        assert "not valid JSON" in result.output
        assert host_config.read_text(encoding="utf-8") == RAW_CONFIG
        assert self._files(host_config) == {host_config.name}

    def test_rejects_schema_invalid_config_without_touching_disk(
        self, runner, host_config
    ):
        result = _invoke(
            runner, host_config, "put", input=json.dumps({"projects": "not a list"})
        )
        assert result.exit_code == 1
        assert "refusing to write an invalid config" in result.output
        assert host_config.read_text(encoding="utf-8") == RAW_CONFIG
        assert self._files(host_config) == {host_config.name}

    def test_rejects_a_project_with_no_path(self, runner, host_config):
        payload = json.dumps({"version": 3, "projects": [{"group": "work"}]})
        result = _invoke(runner, host_config, "put", input=payload)
        assert result.exit_code == 1
        assert host_config.read_text(encoding="utf-8") == RAW_CONFIG

    def test_rejects_empty_stdin(self, runner, host_config):
        result = _invoke(runner, host_config, "put", input="   \n")
        assert result.exit_code == 1
        assert "no config on stdin" in result.output
        assert host_config.read_text(encoding="utf-8") == RAW_CONFIG

    def test_writes_and_round_trips_unknown_keys(self, runner, host_config):
        payload = json.loads(RAW_CONFIG)
        payload["layout"] = {"columns": 4, "rows": 1}
        result = _invoke(runner, host_config, "put", input=json.dumps(payload))
        assert result.exit_code == 0

        written = json.loads(host_config.read_text(encoding="utf-8"))
        assert written["layout"] == {"columns": 4, "rows": 1}
        assert written["aFutureTopLevelKey"] == {"nested": True}
        assert written["projects"][0]["somethingMagentNeverHeardOf"] == 42

    def test_backs_up_the_previous_config_on_success(self, runner, host_config):
        payload = json.loads(RAW_CONFIG)
        payload["baseDir"] = "/srv/projects"
        result = _invoke(runner, host_config, "put", input=json.dumps(payload))
        assert result.exit_code == 0

        backup = host_config.with_name(host_config.name + ".bak-remote-edit")
        assert backup.read_text(encoding="utf-8") == RAW_CONFIG
        assert self._files(host_config) == {host_config.name, backup.name}

    def test_leaves_no_temp_file_behind(self, runner, host_config):
        _invoke(runner, host_config, "put", input=RAW_CONFIG)
        assert not [p for p in host_config.parent.iterdir() if p.suffix == ".tmp"]

    def test_creates_the_config_when_the_host_has_none(self, runner, tmp_path):
        fresh = tmp_path / "sub" / "magent.config.json"
        result = _invoke(runner, fresh, "put", input=RAW_CONFIG)
        assert result.exit_code == 0
        assert json.loads(fresh.read_text(encoding="utf-8"))["baseDir"] == "C:/projects"
        # Nothing to back up on a first write.
        assert not fresh.with_name(fresh.name + ".bak-remote-edit").exists()


class FakeSSH:
    """Stand-in for `attach._ssh_capture`, recording every remote invocation."""

    def __init__(
        self,
        cat: tuple[int, str, str] = (0, RAW_CONFIG, ""),
        put: tuple[int, str, str] = (0, "", ""),
    ) -> None:
        self.cat = cat
        self.put = put
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(
        self,
        target: str,
        remote_cmd: str,
        timeout: int = 30,
        stdin_text: str | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append((target, remote_cmd, stdin_text))
        return self.cat if remote_cmd.endswith("cat") else self.put

    @property
    def pushed(self) -> list[str | None]:
        return [text for _, cmd, text in self.calls if cmd.endswith("put")]


@pytest.fixture
def edits(monkeypatch):
    """Script the blocking editor: it rewrites the temp file with whatever the
    test hands it (None = the user saved nothing), and records the path so a
    test can assert the file survives an abort."""
    seen: dict[str, Path] = {}

    def _script(new_text: str | None):
        def _fake_editor(path: Path) -> None:
            seen["path"] = path
            if new_text is not None:
                path.write_text(new_text, encoding="utf-8")

        monkeypatch.setattr(config_editor, "_edit_and_wait", _fake_editor)
        return seen

    return _script


def _cleanup(seen: dict[str, Path]) -> None:
    if "path" in seen:
        shutil.rmtree(seen["path"].parent, ignore_errors=True)


class TestConfigEditClient:
    """The client half: fetch, edit, validate here, push there."""

    def test_happy_path_fetches_edits_and_pushes(self, runner, monkeypatch, edits):
        edited = json.dumps({**json.loads(RAW_CONFIG), "baseDir": "/srv/projects"})
        seen = edits(edited)
        ssh = FakeSSH()
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 0
        assert [cmd for _, cmd, _ in ssh.calls] == [
            "magent config cat",
            "magent config put",
        ]
        assert {t for t, _, _ in ssh.calls} == {"me@desktop"}
        assert json.loads(ssh.pushed[0])["baseDir"] == "/srv/projects"
        assert "Updated the config on" in result.output
        # The scratch copy is cleaned up once the host has it.
        assert not seen["path"].exists()

    def test_unchanged_edit_pushes_nothing(self, runner, monkeypatch, edits):
        seen = edits(None)
        ssh = FakeSSH()
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 0
        assert ssh.pushed == []
        assert "unchanged" in result.output
        assert not seen["path"].exists()

    def test_invalid_edit_aborts_and_keeps_the_temp_file(
        self, runner, monkeypatch, edits
    ):
        seen = edits("{ oops, not json")
        ssh = FakeSSH()
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 1
        assert ssh.pushed == []
        assert "Not pushing" in result.output
        assert str(seen["path"]) in result.output
        assert seen["path"].read_text(encoding="utf-8") == "{ oops, not json"
        _cleanup(seen)

    def test_schema_invalid_edit_aborts(self, runner, monkeypatch, edits):
        seen = edits(json.dumps({"projects": "not a list"}))
        ssh = FakeSSH()
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 1
        assert ssh.pushed == []
        assert "projects" in result.output
        _cleanup(seen)

    def test_old_host_gets_an_upgrade_message_not_a_traceback(
        self, runner, monkeypatch, edits
    ):
        edits(None)
        ssh = FakeSSH(cat=(2, "", "Error: No such command 'cat'."))
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "too old to read its config remotely" in result.output
        assert "pip install -U magent-multi-ai-agents-manager" in result.output
        assert ssh.pushed == []

    def test_old_host_rejecting_the_push_says_upgrade_too(
        self, runner, monkeypatch, edits
    ):
        seen = edits(json.dumps({**json.loads(RAW_CONFIG), "baseDir": "/srv"}))
        ssh = FakeSSH(put=(2, "", "Error: No such command 'put'."))
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 1
        assert "too old to write its config remotely" in result.output
        assert str(seen["path"]) in result.output
        _cleanup(seen)

    def test_unreachable_host_explains_instead_of_crashing(
        self, runner, monkeypatch, edits
    ):
        edits(None)
        monkeypatch.setattr(attach, "_ssh_capture", FakeSSH(cat=(124, "", "")))
        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])
        assert result.exit_code == 1
        assert "SSH timed out" in result.output

    def test_missing_magent_on_the_host_hints_at_path(self, runner, monkeypatch, edits):
        edits(None)
        monkeypatch.setattr(
            attach,
            "_ssh_capture",
            FakeSSH(cat=(127, "", "bash: magent: command not found")),
        )
        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])
        assert result.exit_code == 1
        assert "on PATH on the host" in result.output

    def test_empty_remote_config_is_not_opened_in_an_editor(
        self, runner, monkeypatch, edits
    ):
        seen = edits(None)
        monkeypatch.setattr(attach, "_ssh_capture", FakeSSH(cat=(0, "\n", "")))
        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])
        assert result.exit_code == 1
        assert "empty config" in result.output
        assert "path" not in seen

    def test_push_rejected_by_the_host_keeps_the_edits(
        self, runner, monkeypatch, edits
    ):
        seen = edits(json.dumps({**json.loads(RAW_CONFIG), "baseDir": "/srv"}))
        ssh = FakeSSH(put=(1, "", "Error: refusing to write an invalid config -- boom"))
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])

        assert result.exit_code == 1
        assert "did not accept the config" in result.output
        assert str(seen["path"]) in result.output
        _cleanup(seen)


class TestRemoteTargetResolution:
    def test_no_host_falls_back_to_the_last_attach_target(
        self, runner, monkeypatch, edits
    ):
        edits(None)
        monkeypatch.setattr(attach, "_read_last_host", lambda: "me@remembered")
        ssh = FakeSSH()
        monkeypatch.setattr(attach, "_ssh_capture", ssh)

        result = runner.invoke(cli.main, ["config", "edit"])

        assert result.exit_code == 0
        assert {t for t, _, _ in ssh.calls} == {"me@remembered"}

    def test_no_host_and_nothing_remembered_is_a_clear_usage_error(
        self, runner, monkeypatch
    ):
        monkeypatch.setattr(attach, "_read_last_host", lambda: None)
        result = runner.invoke(cli.main, ["config", "edit"])
        assert result.exit_code == 1
        assert "no remembered attach host" in result.output
        assert "magent config edit user@host" in result.output

    def test_an_explicit_host_never_reads_the_last_host_memory(
        self, runner, monkeypatch, edits
    ):
        edits(None)

        def _boom() -> str | None:
            raise AssertionError("_read_last_host must not be consulted")

        monkeypatch.setattr(attach, "_read_last_host", _boom)
        monkeypatch.setattr(attach, "_ssh_capture", FakeSSH())

        result = runner.invoke(cli.main, ["config", "edit", "me@desktop"])
        assert result.exit_code == 0
