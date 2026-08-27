"""`magent terminal install` / `magent terminal status` + the wt_keys engine.

Every test drives a settings.json in tmp through the injectable resolver
(`--settings-file`); the real Windows Terminal settings this machine actually
uses are never read and never written.
"""

from __future__ import annotations

import json

import pytest

from magent import cli, wt_keys
from tests.conftest import FakePlatform

# The literal escape TEXT that must land in the file. Written as Python
# escapes of the ASCII backslash so this assertion cannot accidentally become
# "the control character equals itself".
CTRL_W_ESCAPE = "\\u0017"
ESC_CR_ESCAPE = "\\u001b\\r"


@pytest.fixture(autouse=True)
def _on_windows(monkeypatch):
    """Every command in this module is Windows-gated; the unit tier runs on
    three OSes, so the capability probe is faked rather than the platform."""
    monkeypatch.setattr(
        "magent.platform.get_platform",
        lambda: FakePlatform(supports_wt_keybindings=True),
    )


def _settings(tmp_path, doc):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(doc, indent=4), encoding="utf-8")
    return path


def _install(runner, path):
    return runner.invoke(
        cli.main, ["terminal", "install", "--settings-file", str(path)]
    )


def _status(runner, path):
    return runner.invoke(cli.main, ["terminal", "status", "--settings-file", str(path)])


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestSchemaDetection:
    def test_empty_file_is_the_modern_split_schema(self):
        assert wt_keys.detect_schema({}) == wt_keys.SCHEMA_SPLIT

    def test_ids_only_keybindings_is_split(self):
        doc = {
            "actions": [{"command": {"action": "copy"}, "id": "User.copy"}],
            "keybindings": [{"id": "User.copy", "keys": "ctrl+c"}],
        }
        assert wt_keys.detect_schema(doc) == wt_keys.SCHEMA_SPLIT

    def test_inline_actions_entry_is_legacy_actions(self):
        doc = {"actions": [{"command": {"action": "copy"}, "keys": "ctrl+c"}]}
        assert wt_keys.detect_schema(doc) == wt_keys.SCHEMA_ACTIONS_INLINE

    def test_inline_keybindings_entry_is_the_oldest_schema(self):
        doc = {"keybindings": [{"command": "paste", "keys": "ctrl+v"}]}
        assert wt_keys.detect_schema(doc) == wt_keys.SCHEMA_KEYBINDINGS_INLINE


class TestInstallSplitSchema:
    def test_appends_an_action_and_a_keybinding_per_key(self, runner, tmp_path):
        path = _settings(tmp_path, {"profiles": {"defaults": {}}})
        result = _install(runner, path)
        assert result.exit_code == 0

        doc = _read(path)
        assert doc["profiles"] == {"defaults": {}}  # unknown keys round-trip
        ids = {a["id"]: a["command"] for a in doc["actions"]}
        assert ids["User.magent.sendInput.ctrlBackspace"] == {
            "action": "sendInput",
            "input": wt_keys.CTRL_W,
        }
        assert ids["User.magent.sendInput.shiftEnter"] == {
            "action": "sendInput",
            "input": wt_keys.ESC_CR,
        }
        keys = {k["keys"]: k["id"] for k in doc["keybindings"]}
        assert keys["ctrl+backspace"] == "User.magent.sendInput.ctrlBackspace"
        assert keys["shift+enter"] == "User.magent.sendInput.shiftEnter"

    def test_action_ids_are_stable_across_reinstalls(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        _install(runner, path)
        first = sorted(a["id"] for a in _read(path)["actions"])
        _install(runner, path)
        assert sorted(a["id"] for a in _read(path)["actions"]) == first

    def test_control_bytes_land_as_escape_text_not_raw_bytes(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        _install(runner, path)
        written = path.read_text(encoding="utf-8")
        assert CTRL_W_ESCAPE in written
        assert ESC_CR_ESCAPE in written
        # A raw control byte in the file would be invalid JSON and can break WT.
        assert wt_keys.CTRL_W not in written
        assert "\x1b" not in written


class TestInstallLegacySchemas:
    def test_actions_inline_file_gets_an_inline_entry(self, runner, tmp_path):
        path = _settings(
            tmp_path, {"actions": [{"command": {"action": "copy"}, "keys": "ctrl+c"}]}
        )
        assert _install(runner, path).exit_code == 0

        doc = _read(path)
        assert "keybindings" not in doc
        ours = [a for a in doc["actions"] if a.get("keys") == "ctrl+backspace"]
        assert len(ours) == 1
        assert ours[0]["command"] == {"action": "sendInput", "input": wt_keys.CTRL_W}
        assert "id" not in ours[0]

    def test_keybindings_inline_file_gets_an_inline_entry(self, runner, tmp_path):
        path = _settings(
            tmp_path, {"keybindings": [{"command": "paste", "keys": "ctrl+v"}]}
        )
        assert _install(runner, path).exit_code == 0

        doc = _read(path)
        assert "actions" not in doc
        ours = [k for k in doc["keybindings"] if k.get("keys") == "shift+enter"]
        assert ours[0]["command"] == {"action": "sendInput", "input": wt_keys.ESC_CR}


class TestIdempotence:
    def test_rerun_reports_already_installed_and_writes_nothing(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        _install(runner, path)
        before = path.read_text(encoding="utf-8")
        backups = len(list(tmp_path.glob("settings.json.magent-*.bak")))

        result = _install(runner, path)

        assert result.exit_code == 0
        assert "already installed" in result.output.lower()
        assert path.read_text(encoding="utf-8") == before
        # No write means no backup: a no-op run must not litter the directory.
        assert len(list(tmp_path.glob("settings.json.magent-*.bak"))) == backups

    def test_a_second_run_does_not_duplicate_entries(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        _install(runner, path)
        _install(runner, path)
        doc = _read(path)
        assert len(doc["actions"]) == 2
        assert len(doc["keybindings"]) == 2


class TestConflicts:
    def test_a_foreign_binding_is_skipped_and_the_other_key_installs(
        self, runner, tmp_path
    ):
        path = _settings(
            tmp_path,
            {"actions": [{"command": {"action": "copy"}, "keys": "ctrl+backspace"}]},
        )
        result = _install(runner, path)

        assert result.exit_code == 0
        assert "ctrl+backspace" in result.output
        assert "already bound to copy" in result.output

        doc = _read(path)
        inputs = [
            a["command"].get("input")
            for a in doc["actions"]
            if isinstance(a.get("command"), dict)
        ]
        assert wt_keys.CTRL_W not in inputs  # the user's binding won
        assert wt_keys.ESC_CR in inputs  # ...and the other key still installed

    def test_a_conflict_is_seen_through_key_spelling_variants(self):
        doc = {"keybindings": [{"command": "paste", "keys": "Backspace+Ctrl"}]}
        state = wt_keys.binding_state(doc, wt_keys.BINDINGS[0])
        assert state.state == wt_keys.CONFLICT

    def test_a_split_schema_conflict_names_the_action_it_resolves_to(self):
        doc = {
            "actions": [{"command": {"action": "closePane"}, "id": "User.closePane"}],
            "keybindings": [{"id": "User.closePane", "keys": "shift+enter"}],
        }
        state = wt_keys.binding_state(doc, wt_keys.BINDINGS[1])
        assert state.state == wt_keys.CONFLICT
        assert "closePane" in state.detail


class TestJsoncRefusal:
    def test_comments_make_install_refuse_and_print_the_manual_snippet(
        self, runner, tmp_path
    ):
        path = tmp_path / "settings.json"
        original = '{\n  // Windows Terminal allows comments\n  "actions": [],\n}\n'
        path.write_text(original, encoding="utf-8")

        result = _install(runner, path)

        assert result.exit_code == 1
        assert path.read_text(encoding="utf-8") == original  # never written
        assert not list(tmp_path.glob("*.bak"))
        assert str(path) in result.output
        assert "sendInput" in result.output
        assert CTRL_W_ESCAPE in result.output
        assert ESC_CR_ESCAPE in result.output

    def test_status_reports_unreadable_without_writing(self, runner, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{ /* jsonc */ }", encoding="utf-8")
        result = _status(runner, path)
        assert result.exit_code == 0
        assert "unreadable" in result.output


class TestBackup:
    def test_a_timestamped_backup_holds_the_original_bytes(self, runner, tmp_path):
        path = _settings(tmp_path, {"profiles": {"list": []}})
        original = path.read_text(encoding="utf-8")

        _install(runner, path)

        backups = list(tmp_path.glob("settings.json.magent-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original

    def test_backup_name_carries_the_timestamp(self, tmp_path):
        path = tmp_path / "settings.json"
        assert wt_keys.backup_path(path, 0).name.startswith("settings.json.magent-")
        assert wt_keys.backup_path(path, 0).name.endswith(".bak")


class TestStatusCommand:
    def test_reports_missing_with_the_repair_hint(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        result = _status(runner, path)
        assert result.exit_code == 0
        assert "ctrl+backspace" in result.output
        assert "shift+enter" in result.output
        assert "magent terminal install" in result.output
        assert _read(path) == {}  # status never writes

    def test_reports_installed_without_a_repair_hint(self, runner, tmp_path):
        path = _settings(tmp_path, {})
        _install(runner, path)
        result = _status(runner, path)
        assert result.exit_code == 0
        assert "magent terminal install" not in result.output


class TestResolution:
    def test_candidates_cover_store_preview_and_unpackaged(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\LA")
        names = [str(p) for p in wt_keys.candidate_paths()]
        assert any("Microsoft.WindowsTerminal_8wekyb3d8bbwe" in n for n in names)
        assert any("Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe" in n for n in names)
        assert any("Windows Terminal" in n for n in names)

    def test_first_existing_candidate_wins(self, tmp_path, monkeypatch):
        preview = (
            tmp_path
            / "Packages"
            / "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"
            / "LocalState"
        )
        preview.mkdir(parents=True)
        (preview / "settings.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            wt_keys,
            "candidate_paths",
            lambda: [
                tmp_path / "nope" / "settings.json",
                preview / "settings.json",
            ],
        )
        assert wt_keys.find_settings() == preview / "settings.json"

    def test_no_candidate_reports_windows_terminal_not_found(self, runner, monkeypatch):
        monkeypatch.setattr(wt_keys, "candidate_paths", list)
        result = runner.invoke(cli.main, ["terminal", "install"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestNonWindows:
    def test_install_says_windows_only(self, runner, monkeypatch, tmp_path):
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        result = _install(runner, tmp_path / "settings.json")
        assert result.exit_code == 1
        assert "Windows-only" in result.output

    def test_status_says_windows_only_and_stays_green(
        self, runner, monkeypatch, tmp_path
    ):
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        result = _status(runner, tmp_path / "settings.json")
        assert result.exit_code == 0
        assert "Windows-only" in result.output
