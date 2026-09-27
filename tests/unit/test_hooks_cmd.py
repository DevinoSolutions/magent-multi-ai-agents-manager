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


# The pip-rollback-proof spelling a real machine is wired with: the same writer
# run as a module, so no console script has to survive an upgrade.
MODULE_CMD = "py -3.14 -m magent.state_hook --source claude"


def _write_module_form(settings_file, cmd=MODULE_CMD):
    """Every event wired in module form, the shape install itself writes."""
    hooks = {}
    for event in EVENTS:
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if event == "PostToolUse":
            entry = {"matcher": "*", **entry}
        hooks[event] = [entry]
    settings_file.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return hooks


# What every repair is: true of a pre-3.1.2 console-script path and of a
# drive-letter module-form interpreter path alike, unlike a version-specific
# origin -- and unlike "bash cannot run", which a double-quoted one can.
REPAIR_SUFFIX = "(Windows backslash path)"


def _repaired_line(output):
    """The install report's one "Repaired ..." line (CliRunner strips styles)."""
    lines = [ln for ln in output.splitlines() if "Repaired" in ln]
    assert len(lines) == 1, output
    return lines[0]


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

    def test_reinstall_repairs_backslash_command(self, runner, tmp_path, monkeypatch):
        # A pre-3.1.2 install wired backslash paths bash cannot run; the
        # marker-based idempotence must not skip them -- reinstall rewrites
        # them to THIS install's command, not a slash-swapped copy of the old.
        monkeypatch.setattr(
            hooks_cmd.shutil, "which", lambda _: "C:/new/magent-state-hook.EXE"
        )
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
        assert _repaired_line(result.output).endswith(REPAIR_SUFFIX)
        data = json.loads(settings.read_text(encoding="utf-8"))
        cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
        ours = [c for c in cmds if "magent-state-hook" in c]
        assert ours == ["C:/new/magent-state-hook.EXE --source claude"]
        assert "node notify.mjs" in cmds  # foreign hook untouched

    def test_unc_console_script_is_repaired(self, runner, tmp_path, monkeypatch):
        # A pre-3.1.2 --user install under a folder-redirected AppData wrote a
        # UNC path: no drive letter, and bash eats it all the same -- so unlike
        # the module-form swap, the console-script rewrite needs no drive letter.
        monkeypatch.setattr(
            hooks_cmd.shutil, "which", lambda _: "C:/new/magent-state-hook.EXE"
        )
        stale = (
            r"\\corp-fs\home$\me\AppData\Roaming\Python\Python314\Scripts"
            r"\magent-state-hook.exe --source claude"
        )
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": stale}]}]
                    }
                }
            ),
            encoding="utf-8",
        )
        result = _install(runner, settings)
        assert "Repaired" in result.output
        data = json.loads(settings.read_text(encoding="utf-8"))
        cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
        assert cmds == ["C:/new/magent-state-hook.EXE --source claude"]

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

    @pytest.mark.parametrize(
        "ours",
        [
            pytest.param(MODULE_CMD, id="module-form"),
            pytest.param(
                r"c:\users\x\scripts\magent-state-hook.EXE --source claude",
                id="stale-console-script",
            ),
        ],
    )
    def test_foreign_backslash_hook_beside_ours_is_untouched(
        self, runner, tmp_path, ours
    ):
        # A wired event is walked hook by hook for repair; a foreign hook with
        # a Windows path (anotifier's `node "C:\..."`) is not ours to rewrite.
        foreign = r'node "C:\ProgramData\anotifier\notify.mjs" --event stop'
        settings = tmp_path / "settings.json"
        hooks = {
            event: [
                {"hooks": [{"type": "command", "command": ours}]},
                {"hooks": [{"type": "command", "command": foreign}]},
            ]
            for event in EVENTS
        }
        settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        assert _install(runner, settings).exit_code == 0
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event in EVENTS:
            cmds = [h["command"] for e in data["hooks"][event] for h in e["hooks"]]
            assert cmds.count(foreign) == 1, cmds

    def test_prints_codex_recipe(self, runner, tmp_path):
        result = _install(runner, tmp_path / "settings.json")
        assert "notify = [" in result.output and "--source" in result.output

    def test_corrupt_settings_exits_one(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("not json {", encoding="utf-8")
        result = _install(runner, settings)
        assert result.exit_code == 1
        assert settings.read_text(encoding="utf-8") == "not json {"

    def test_module_form_is_not_duplicated(self, runner, tmp_path):
        # A module-form entry IS the state hook: adding the console script
        # beside it would run the writer twice per event.
        settings = tmp_path / "settings.json"
        before = _write_module_form(settings)
        result = _install(runner, settings)
        assert result.exit_code == 0
        assert "Already wired" in result.output
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["hooks"] == before
        assert "magent-state-hook" not in settings.read_text(encoding="utf-8")
        again = _install(runner, settings)
        assert "Already wired" in again.output
        assert json.loads(settings.read_text(encoding="utf-8"))["hooks"] == before

    @pytest.mark.parametrize(
        ("stale", "fixed"),
        [
            pytest.param(
                r'"C:\Program Files\Python314\python.exe" -X utf8 '
                "-m magent.state_hook --source claude",
                '"C:/Program Files/Python314/python.exe" -X utf8 '
                "-m magent.state_hook --source claude",
                id="quoted",
            ),
            pytest.param(
                r"c:\python314\python.exe -m magent.state_hook --source claude",
                "c:/python314/python.exe -m magent.state_hook --source claude",
                id="unquoted-lowercase-drive",
            ),
        ],
    )
    def test_reinstall_repairs_backslash_module_form(
        self, runner, tmp_path, stale, fixed
    ):
        # Recognising the module form must not let a broken one hide behind
        # idempotence -- but the repair fixes only what bash breaks. The module
        # spelling exists to avoid the console script, so it stays a module
        # command: backslashes become forward slashes, every other byte kept.
        settings = tmp_path / "settings.json"
        _write_module_form(settings, cmd=stale)
        result = _install(runner, settings)
        assert result.exit_code == 0
        assert "Repaired" in result.output
        assert _repaired_line(result.output).endswith(REPAIR_SUFFIX)
        data = json.loads(settings.read_text(encoding="utf-8"))
        for event in EVENTS:
            cmds = [h["command"] for e in data["hooks"][event] for h in e["hooks"]]
            assert cmds == [fixed]
        again = _install(runner, settings)
        assert "Already wired" in again.output
        assert json.loads(settings.read_text(encoding="utf-8")) == data

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param(
                r"/Users/me/My\ Venv/bin/python -m magent.state_hook --source claude",
                id="escaped-space",
            ),
            # The interpreter's venv named for the hook: still module form, so
            # it must not fall through to the console-script rewrite either.
            pytest.param(
                r"/opt/magent-state-hook\ env/bin/python "
                "-m magent.state_hook --source claude",
                id="hook-named-venv",
            ),
        ],
    )
    def test_posix_escaped_module_form_is_left_alone(self, runner, tmp_path, cmd):
        # Off a drive letter a backslash is bash escape syntax, not a Windows
        # separator: `My\ Venv` runs, and swapping it to `My/ Venv` would turn
        # a working hook into rc 127. Nothing to repair -- byte for byte.
        settings = tmp_path / "settings.json"
        before = _write_module_form(settings, cmd=cmd)
        result = _install(runner, settings)
        assert result.exit_code == 0
        assert "Already wired" in result.output
        assert "Repaired" not in result.output
        assert json.loads(settings.read_text(encoding="utf-8"))["hooks"] == before


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

    def test_module_form_reports_wired(self, runner, tmp_path):
        settings = tmp_path / "settings.json"
        _write_module_form(settings)
        result = runner.invoke(
            cli.main, ["hooks", "status", "--settings-file", str(settings)]
        )
        assert result.exit_code == 0
        for event in EVENTS:
            assert f"+ {event}\n" in result.output
            assert f"x {event}\n" not in result.output
