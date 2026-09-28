"""`magent hooks install` / `magent hooks status` -- idempotent merge into
Claude Code's settings.json, preservation of foreign hooks, and the status
report over the wired events + state store.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from magent import agent_state, cli
from magent.cli import hooks_cmd
from magent.style import style

EVENTS = list(hooks_cmd._EVENTS)

# A settings file nested past the JSON parser's depth: json.loads raises
# RecursionError on it, which is not a ValueError.
_NESTED = '{"hooks": ' + "[" * 200_000 + "]" * 200_000 + "}"


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


# What every repair fixes, console-script and module form alike.
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

    def test_settings_nested_past_the_parsers_depth_exits_one(self, runner, tmp_path):
        # json.loads raises RecursionError there, not ValueError: it must be
        # the same named refusal, never a traceback, and the file is untouched.
        settings = tmp_path / "settings.json"
        settings.write_text(_NESTED, encoding="utf-8")
        result = _install(runner, settings)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert f"Cannot edit {settings}: " in result.stderr
        assert "nested too deeply" in result.stderr
        assert settings.read_text(encoding="utf-8") == _NESTED

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


# A settings.json can carry API keys in its "env" block. Every unusable file
# below carries this one, to prove none of it survives in the refusal.
_SECRET = "sentinel-secret-9d2a"
_ENV = b'"env": {"ANTHROPIC_API_KEY": "' + _SECRET.encode() + b'"}, '

_UNUSABLE = [
    pytest.param(
        b"{" + _ENV + b"not json", "not valid JSON (JSONDecodeError)", id="not-json"
    ),
    pytest.param(
        b"{" + _ENV + b'"hooks": "\xff"}',
        "not valid UTF-8 (UnicodeDecodeError)",
        id="not-utf8",
    ),
    pytest.param(
        b"{" + _ENV + _NESTED[1:].encode(),
        "nested too deeply to parse (RecursionError)",
        id="nested",
    ),
    pytest.param(b"[{" + _ENV[:-2] + b"}]", "not a JSON object", id="not-an-object"),
    pytest.param(
        b"{" + _ENV + b'"hooks": [], "model": "keep-me"}',
        '"hooks" is not a JSON object',
        id="hooks-not-an-object",
    ),
    pytest.param(
        b"{" + _ENV + b'"hooks": {"Stop": {"type": "command", "command": "mine"}}}',
        '"hooks.Stop" is not a JSON array',
        id="event-not-an-array",
    ),
    # A directory where the file should be: a real OSError on every OS.
    pytest.param(
        None,
        "could not be read "
        f"({'PermissionError' if sys.platform == 'win32' else 'IsADirectoryError'})",
        id="unreadable",
    ),
]


def _where_the_secret_survives(exc: BaseException | None) -> list[str]:
    """Each place _SECRET can be reached from ``exc``: every link of its
    chain (__cause__ AND __context__, suppressed or not), each link's args and
    decode ``object``, and every local of every frame on each link's traceback
    -- followed into dicts, lists and tuples, since a parsed settings file is
    a dict."""
    hits: list[str] = []
    keep: list[object] = []  # holds every visited value, so no id is reused
    seen: set[int] = set()
    pending: list[object] = [exc]
    while pending:
        value = pending.pop()
        if value is None or id(value) in seen:
            continue
        seen.add(id(value))
        keep.append(value)
        if isinstance(value, str | bytes):
            needle = _SECRET.encode() if isinstance(value, bytes) else _SECRET
            if needle in value:
                hits.append(ascii(value)[:60])
        elif isinstance(value, dict):
            pending += [*value.keys(), *value.values()]
        elif isinstance(value, list | tuple):
            pending += list(value)
        elif isinstance(value, BaseException):
            pending += [*value.args, getattr(value, "object", None)]
            pending += [value.__cause__, value.__context__]
            tb = value.__traceback__
            while tb is not None:
                pending += list(tb.tb_frame.f_locals.values())
                tb = tb.tb_next
    return hits


class TestASettingsFileMagentCannotUnderstand:
    """status used to read any of these as "every event unwired", and install
    either died with a traceback (OSError) or quietly REWROTE the file,
    replacing a non-object ``hooks`` or a non-array event value with its own.
    The wt_keys law applies: a file magent cannot safely understand is
    refused, never rewritten -- and never reported as something it is not."""

    @staticmethod
    def _plant(tmp_path, content):
        settings = tmp_path / "claude" / "settings.json"
        settings.parent.mkdir()
        if content is None:
            settings.mkdir()
        else:
            settings.write_bytes(content)
        return settings

    @pytest.mark.parametrize(("content", "reason"), _UNUSABLE)
    def test_install_refuses_and_leaves_it_byte_identical(
        self, runner, tmp_path, content, reason
    ):
        settings = self._plant(tmp_path, content)

        result = _install(runner, settings)

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stdout == ""
        assert result.stderr == f"  x Cannot edit {settings}: {reason}\n"
        # Our words alone: a parser error's .doc is the whole file, and a
        # frame that parsed it holds it as a local.
        assert _where_the_secret_survives(result.exception) == []
        if content is None:
            assert list(settings.iterdir()) == []
        else:
            assert settings.read_bytes() == content
        # Nothing written beside it either: no temp file, no backup.
        assert [p.name for p in settings.parent.iterdir()] == ["settings.json"]

    @pytest.mark.parametrize(("content", "reason"), _UNUSABLE)
    def test_status_names_the_problem_and_exits_one(
        self, runner, tmp_path, content, reason
    ):
        # Unknown is not success: a script reading `magent hooks status`'s
        # exit code must not take "cannot tell" for "all wired".
        settings = self._plant(tmp_path, content)

        result = runner.invoke(
            cli.main, ["hooks", "status", "--settings-file", str(settings)]
        )

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stderr == (
            f"  x {settings}: {reason}; cannot tell which hooks are wired\n"
        )
        for event in EVENTS:
            assert f"x {event}" not in result.output
            assert f"+ {event}" not in result.output
        # The store half of the report still runs.
        assert "State store is empty" in result.stdout
        assert _where_the_secret_survives(result.exception) == []

    def test_the_status_refusal_mark_is_red(self, runner, tmp_path):
        settings = self._plant(tmp_path, b"not json {")

        result = runner.invoke(
            cli.main, ["hooks", "status", "--settings-file", str(settings)], color=True
        )

        assert result.stderr.startswith(f"  {style('x', fg='red')} {settings}: ")


# A settings.json install can read and wire, holding a key it must not leak.
_VALID = b"{" + _ENV + b'"model": "keep-me"}'
# Planted as its mtime: far enough in the past that any touch shows.
_OLD_MTIME_NS = 10**18


class TestASettingsFileMagentCannotWrite:
    """install used to die on the write side with a traceback -- measured: a
    read-only settings.json on Windows fails os.replace with PermissionError
    -- and left its settings.tmp behind. A write that fails is refused the way
    a read that fails is: our words, exit 1, the file byte-identical, nothing
    beside it."""

    @staticmethod
    def _plant(tmp_path):
        settings = tmp_path / "claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_bytes(_VALID)
        os.utime(settings, ns=(_OLD_MTIME_NS, _OLD_MTIME_NS))
        return settings

    @staticmethod
    def _assert_refused_untouched(result, settings):
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.stdout == ""
        assert result.stderr == (
            f"  x Cannot edit {settings}: could not be written (PermissionError)\n"
        )
        assert _where_the_secret_survives(result.exception) == []
        assert settings.read_bytes() == _VALID
        # The writability probe opens the file r+b and closes it: no
        # truncation, no write, so not even the mtime moves.
        assert settings.stat().st_mtime_ns == _OLD_MTIME_NS
        assert [p.name for p in settings.parent.iterdir()] == ["settings.json"]

    def test_a_failed_replace_removes_the_temp_file(
        self, runner, tmp_path, monkeypatch
    ):
        settings = self._plant(tmp_path)
        real_replace = os.replace
        temp_files = []

        def replace(src, dst):
            if Path(dst) != settings:
                return real_replace(src, dst)
            # Recorded so the cleanup assertion is not vacuous: the temp file
            # really was written before the replace failed.
            temp_files.append((Path(src).name, Path(src).is_file()))
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(os, "replace", replace)

        result = _install(runner, settings)

        assert temp_files == [("settings.tmp", True)]
        self._assert_refused_untouched(result, settings)

    def test_a_read_only_file_is_refused_untouched(self, runner, tmp_path):
        settings = self._plant(tmp_path)
        # 0444 on POSIX; on Windows it sets the read-only attribute.
        settings.chmod(0o444)
        try:
            if os.access(settings, os.W_OK):
                pytest.skip("this user can write a read-only file (root)")
            result = _install(runner, settings)
            # Still read-only: POSIX renames over a 0444 file as freely as
            # over any other, and the replacement would come back writable.
            assert not os.access(settings, os.W_OK)
        finally:
            settings.chmod(0o644)
        self._assert_refused_untouched(result, settings)

    def test_a_read_only_file_is_refused_where_the_rename_would_succeed(
        self, runner, tmp_path, monkeypatch
    ):
        # POSIX's rename, stood in on every OS: it ignores the destination's
        # mode. The refusal must not lean on Windows refusing the replace.
        settings = self._plant(tmp_path)
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == settings:
                os.chmod(dst, 0o644)
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        settings.chmod(0o444)
        try:
            if os.access(settings, os.W_OK):
                pytest.skip("this user can write a read-only file (root)")
            result = _install(runner, settings)
            assert not os.access(settings, os.W_OK)
        finally:
            settings.chmod(0o644)
        self._assert_refused_untouched(result, settings)


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
