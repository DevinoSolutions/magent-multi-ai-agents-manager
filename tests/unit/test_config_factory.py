from __future__ import annotations

import json
from pathlib import Path

import pytest

from magent.config import (
    DEFAULT_TOOLS,
    SCHEMA_VERSION,
    ConfigError,
    LayoutConfig,
    Settings,
    _migrate_2_to_3,
    _parse_settings,
    default_config,
    layout_to_dict,
    load_config,
    migrate_config_file,
    settings_to_dict,
)
from magent.discover import projects_to_config
from magent.init_config import generate_config

EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "magent.config.example.json"


class TestFactoryRoundtrip:
    def test_factory_roundtrip(self):
        # Anti-drift pin (R9): settings_to_dict and _parse_settings sit on
        # the same Settings dataclass, so a round-trip must be lossless.
        assert _parse_settings(settings_to_dict(Settings())) == Settings()

    def test_layout_to_dict(self):
        assert layout_to_dict(LayoutConfig()) == {"columns": 2, "rows": 1}

    def test_settings_to_dict_has_full_tools_map(self):
        assert settings_to_dict(Settings())["tools"] == dict(DEFAULT_TOOLS)


class TestGenerateConfigHasVersionAndFullSettings:
    def test_generate_config_has_version_and_full_settings(self, tmp_path):
        (tmp_path / "api" / ".git").mkdir(parents=True)
        config = generate_config(str(tmp_path))
        assert config["version"] == SCHEMA_VERSION
        # F-D6 divergence this factory kills: generators used to emit a
        # reduced 2-tool settings block missing happy/psmux/ssh/upload*.
        assert config["settings"]["tools"] == dict(DEFAULT_TOOLS)
        assert "happy" in config["settings"]
        assert "psmux" in config["settings"]
        assert "ssh" in config["settings"]
        assert "uploadServer" in config["settings"]
        assert "uploadPort" in config["settings"]


class TestProjectsToConfigUsesFactoryEnvelope:
    def test_projects_to_config_uses_factory_envelope(self, tmp_path):
        proj = tmp_path / "group" / "app"
        proj.mkdir(parents=True)
        projects = [
            {"path": str(proj), "tool": "claude", "session_count": 1, "last_active": 1}
        ]
        config = projects_to_config(projects)
        assert config["version"] == SCHEMA_VERSION
        assert config["settings"]["tools"] == dict(DEFAULT_TOOLS)


class TestSingleSourceSettingsBlock:
    def test_single_source_settings_block(self, tmp_path):
        # generate_config, projects_to_config, and default_config must all
        # agree on settings/layout -- they're the same envelope now (R9).
        (tmp_path / "api" / ".git").mkdir(parents=True)
        generated = generate_config(str(tmp_path))

        proj = tmp_path / "group" / "app2"
        proj.mkdir(parents=True)
        discovered = projects_to_config(
            [
                {
                    "path": str(proj),
                    "tool": "claude",
                    "session_count": 1,
                    "last_active": 1,
                }
            ]
        )

        factory = default_config([])

        assert generated["settings"] == discovered["settings"] == factory["settings"]
        assert generated["layout"] == discovered["layout"] == factory["layout"]


class TestMigrateConfigFile:
    def test_migrate_stamps_version_and_persists_colors(self, tmp_config):
        path = tmp_config(
            {
                "projects": [{"path": "api"}, {"path": "web", "color": "#123456"}],
            }
        )

        changed = migrate_config_file(path)

        assert changed is True
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == SCHEMA_VERSION
        assert all("color" in p for p in data["projects"])
        assert data["projects"][1]["color"] == "#123456"  # pre-existing color untouched

        # A subsequent load_config must still write nothing (R10 stays pure
        # even for a file migrate just persisted to).
        before = Path(path).read_bytes()
        load_config(path)
        after = Path(path).read_bytes()
        assert before == after

    def test_migrate_1_to_2_materializes_attention(self, tmp_config):
        path = tmp_config(
            {
                "version": 1,
                "settings": {"defaultTool": "claude"},
                "projects": [{"path": "api", "color": "#111111"}],
            }
        )

        assert migrate_config_file(path) is True

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == SCHEMA_VERSION
        assert data["settings"]["attention"] == {
            "badge": True,
            "flash": True,
            "toast": False,
            "ntfy": False,
        }

    def test_migrate_is_idempotent(self, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [{"path": "api", "color": "#111111"}],
            }
        )
        assert migrate_config_file(path) is False

    def test_migrate_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            migrate_config_file("/nonexistent/config.json")

    def test_migrate_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json{{{")
        with pytest.raises(ConfigError, match="valid JSON"):
            migrate_config_file(str(p))


class TestMigrate2To3Windows:
    """Characterization pins for _migrate_2_to_3, which normalizes the v2
    ``windows`` field (``int | list[str]``) into the v3 array-of-objects form.
    These document the EXACT current behavior of every input shape so a future
    change to the migration surfaces as a visible, deliberate diff -- including
    the shapes the migration deliberately leaves alone."""

    @staticmethod
    def _windows_after(windows: object) -> object:
        raw = _migrate_2_to_3(
            {"version": 2, "projects": [{"path": "api", "windows": windows}]}
        )
        projects = raw["projects"]
        assert isinstance(projects, list)
        project = projects[0]
        assert isinstance(project, dict)
        return project.get("windows", "<absent>")

    def test_int_count_expands_to_empty_objects(self):
        assert self._windows_after(3) == [{}, {}, {}]

    def test_list_of_strings_becomes_name_objects(self):
        assert self._windows_after(["a", "b"]) == [{"name": "a"}, {"name": "b"}]

    @pytest.mark.parametrize("flag", [True, False])
    def test_bool_deletes_windows_key(self, flag):
        assert self._windows_after(flag) == "<absent>"

    @pytest.mark.parametrize("count", [1, 0, -1])
    def test_int_not_greater_than_one_is_left_unchanged(self, count):
        # Documented current behavior: only int > 1 expands; 1/0/negative pass
        # through untouched (and parse to windows=None downstream).
        assert self._windows_after(count) == count

    def test_v3_array_of_objects_passes_through_unchanged(self):
        # Idempotent: already-v3 shapes survive a re-run byte-for-byte.
        assert self._windows_after([{}, {"tool": "codex"}]) == [{}, {"tool": "codex"}]

    def test_version_is_stamped_to_three(self):
        raw = _migrate_2_to_3({"version": 2, "projects": []})
        assert raw["version"] == 3


class TestExampleConfigMatchesFactory:
    def test_example_config_matches_factory(self, tmp_path, capsys):
        with open(EXAMPLE_CONFIG_PATH, encoding="utf-8") as f:
            example = json.load(f)

        assert example["version"] == SCHEMA_VERSION
        assert example["settings"] == settings_to_dict(Settings())
        assert example["layout"] == layout_to_dict(LayoutConfig())
        # Teaches the remote/group/color/tool/enabled surfaces, not just defaults.
        assert any("host" in p for p in example["projects"])
        assert any("remotePath" in p for p in example["projects"])
        assert any("group" in p for p in example["projects"])
        assert any(p.get("enabled") is False for p in example["projects"])
        assert all("color" in p for p in example["projects"])
        assert any("tool" in p for p in example["projects"])

        # Round-trip through the public loader -- factory-dict equality alone
        # doesn't exercise the path real users hit (NF from MINOR's dropped pin).
        copy_path = tmp_path / "magent.config.json"
        copy_path.write_text(json.dumps(example), encoding="utf-8")
        cfg = load_config(str(copy_path))

        assert cfg.version == SCHEMA_VERSION
        assert len(cfg.projects) == len(example["projects"])
        assert capsys.readouterr().err == ""
