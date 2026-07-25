"""`magent hooks install` / `magent hooks status` -- idempotent merge into
Claude Code's settings.json, preservation of foreign hooks, and the status
report over the wired events + state store.
"""

from __future__ import annotations

import json

import pytest

from magent import agent_state, cli
from magent.cli import hooks_cmd

EVENTS = list(hooks_cmd._EVENTS)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(agent_state, "_swept_this_process", False)
    monkeypatch.setattr(agent_state, "_warned_files", set())


def _install(runner, settings_file):
    return runner.invoke(
        cli.main, ["hooks", "install", "--settings-file", str(settings_file)]
    )


class TestInstall:
    def test_fresh_file_wires_every_event(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        result = _install(runner, settings)
        assert result.exit_code == 0
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event in EVENTS:
            entries = data["hooks"][event]
            assert any("magent-state-hook" in json.dumps(e) for e in entries)

    def test_command_carries_source_claude(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _install(runner, settings)
        data = json.loads(settings.read_text(encoding="utf-8"))
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "magent-state-hook" in cmd and "--source claude" in cmd

    def test_command_is_bash_safe_forward_slashes(self, monkeypatch):
        # Claude Code runs hook commands through a POSIX shell even on Windows:
        # a backslash path is eaten as escapes ("c:usersamind..." -> not found).
        monkeypatch.setattr(
            hooks_cmd.shutil,
            "which",
            lambda _: r"C:\Users\x\Scripts\magent-state-hook.EXE",
        )
        assert (
            hooks_cmd._hook_command()
            == "C:/Users/x/Scripts/magent-state-hook.EXE --source claude"
        )
        assert "\\" not in hooks_cmd._codex_recipe()

    def test_reinstall_repairs_backslash_command(self, runner, tmp_path):
        # A pre-3.1.2 install wired backslash paths bash cannot run; the
        # marker-based idempotence must not skip them -- reinstall rewrites.
        settings = tmp_path / "settings.json"
        stale = r"c:\users\x\scripts\magent-state-hook.EXE --source claude"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": stale}]},
                            {
                                "hooks": [
                                    {"type": "command", "command": "node notify.mjs"}
                                ]
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        result = _install(runner, settings)
        assert result.exit_code == 0
        assert "Repaired" in result.output
        data = json.loads(settings.read_text(encoding="utf-8"))
        cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
        ours = [c for c in cmds if "magent-state-hook" in c]
        assert len(ours) == 1 and "\\" not in ours[0]
        assert "node notify.mjs" in cmds  # foreign hook untouched

    def test_reinstall_healthy_reports_already_wired(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _install(runner, settings)
        result = _install(runner, settings)
        assert "Already wired" in result.output
        assert "Repaired" not in result.output

    def test_command_with_spaces_is_quoted(self, monkeypatch):
        monkeypatch.setattr(
            hooks_cmd.shutil,
            "which",
            lambda _: r"C:\Program Files\magent\magent-state-hook.EXE",
        )
        assert (
            hooks_cmd._hook_command()
            == '"C:/Program Files/magent/magent-state-hook.EXE" --source claude'
        )

    def test_post_tool_use_gets_wildcard_matcher(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _install(runner, settings)
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["hooks"]["PostToolUse"][0]["matcher"] == "*"
        assert "matcher" not in data["hooks"]["Stop"][0]

    def test_idempotent_second_run_adds_nothing(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _install(runner, settings)
        before = settings.read_text(encoding="utf-8")
        result = _install(runner, settings)
        assert result.exit_code == 0
        assert "Already wired" in result.output
        assert settings.read_text(encoding="utf-8") == before

    def test_foreign_hooks_are_preserved(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [
                            {
                                "matcher": "",
                                "hooks": [
                                    {"type": "command", "command": "node notify.mjs"}
                                ],
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        _install(runner, settings)
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["model"] == "opus"
        stop_cmds = json.dumps(data["hooks"]["Stop"])
        assert "notify.mjs" in stop_cmds and "magent-state-hook" in stop_cmds

    def test_prints_codex_recipe(self, runner, tmp_path):
        result = _install(runner, tmp_path / "settings.json")
        assert "notify = [" in result.output and "--source" in result.output

    def test_corrupt_settings_exits_one(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("not json {", encoding="utf-8")
        result = _install(runner, settings)
        assert result.exit_code == 1
        assert settings.read_text(encoding="utf-8") == "not json {"


class TestStatus:
    def test_unwired_events_marked_and_empty_store_reported(self, runner, tmp_path):
        result = runner.invoke(
            cli.main,
            ["hooks", "status", "--settings-file", str(tmp_path / "settings.json")],
        )
        assert result.exit_code == 0
        for event in EVENTS:
            assert event in result.output
        assert "State store is empty" in result.output

    def test_wired_events_and_records_reported(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _install(runner, settings)
        agent_state.write_state("/projects/foo", "working")
        result = runner.invoke(
            cli.main, ["hooks", "status", "--settings-file", str(settings)]
        )
        assert result.exit_code == 0
        assert "state record(s)" in result.output
        assert "State store is empty" not in result.output
