from __future__ import annotations

import json
from pathlib import Path

import pytest

from magent import config_io
from magent.cli import main
from magent.config import (
    _MIGRATIONS,
    DEFAULT_TOOLS,
    SCHEMA_VERSION,
    ConfigError,
    LayoutConfig,
    Settings,
    _migrate_2_to_3,
    _migrate_3_to_4,
    _parse_settings,
    default_config,
    layout_to_dict,
    load_config,
    migrate_config_text,
    migrate_raw,
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


class TestMigrateConfigText:
    """The pure half of ``magent config migrate``: text in, migrated dict and
    a changed flag out, nothing on disk."""

    def test_migrate_stamps_version_and_backfills_colors(self):
        raw, changed = migrate_config_text(
            json.dumps(
                {"projects": [{"path": "api"}, {"path": "web", "color": "#123456"}]}
            )
        )

        assert changed is True
        assert raw["version"] == SCHEMA_VERSION
        projects = raw["projects"]
        assert isinstance(projects, list)
        assert all("color" in p for p in projects)
        assert projects[1]["color"] == "#123456"  # pre-existing color untouched

    def test_migrate_1_to_2_materializes_attention(self):
        raw, changed = migrate_config_text(
            json.dumps(
                {
                    "version": 1,
                    "settings": {"defaultTool": "claude"},
                    "projects": [{"path": "api", "color": "#111111"}],
                }
            )
        )

        assert changed is True
        assert raw["version"] == SCHEMA_VERSION
        settings = raw["settings"]
        assert isinstance(settings, dict)
        assert settings["attention"] == {
            "badge": True,
            "flash": True,
            "toast": False,
            "ntfy": False,
        }

    def test_migrate_is_idempotent(self):
        current = {
            "version": SCHEMA_VERSION,
            "projects": [{"path": "api", "color": "#111111"}],
        }
        raw, changed = migrate_config_text(json.dumps(current))
        assert changed is False
        assert raw == current

    def test_migrate_keeps_unknown_keys(self):
        raw, _changed = migrate_config_text(
            json.dumps({"projects": [], "settings": {"someFutureKey": 1}})
        )
        settings = raw["settings"]
        assert isinstance(settings, dict)
        assert settings["someFutureKey"] == 1

    def test_migrate_invalid_json_raises(self):
        with pytest.raises(ConfigError, match="valid JSON"):
            migrate_config_text("not json{{{")

    @pytest.mark.parametrize("color", [None, "#22c55e"], ids=["colorless", "colored"])
    def test_migrate_refuses_text_with_no_utf8_form(self, color):
        # F-SUR-1: the same refusal load_config gives, before migrate's own
        # color hash (colorless: it used to raise the codec's words).
        project: dict[str, object] = {"path": "api", "title": "api\ud83d"}
        if color:
            project["color"] = color
        with pytest.raises(ConfigError) as exc:
            migrate_config_text(json.dumps({"projects": [project]}))
        assert str(exc.value) == (
            "projects[0].title has text with no UTF-8 form (UnicodeEncodeError):"
            " 'api\\ud83d'"
        )

    def test_migrate_refuses_before_it_reshapes_anything(self):
        # The refusal names the field the FILE holds. A v2 legacy window is a
        # bare string; checked after migrate_raw, it would be named by its v3
        # shape, projects[0].windows[0].name, which the file does not contain.
        with pytest.raises(ConfigError) as exc:
            migrate_config_text(
                json.dumps(
                    {
                        "version": 2,
                        "projects": [{"path": "api", "windows": ["x\ud83d"]}],
                    }
                )
            )
        assert str(exc.value) == (
            "projects[0].windows[0] has text with no UTF-8 form (UnicodeEncodeError):"
            " 'x\\ud83d'"
        )


class TestConfigMigrateCommand:
    """``magent config migrate``: the disk half, written through
    ``config_io.save`` (validated, backed up, atomic) under the config lock."""

    def test_migrate_writes_the_file_and_a_backup(self, runner, tmp_config):
        path = tmp_config(
            {"projects": [{"path": "api"}, {"path": "web", "color": "#123456"}]}
        )
        before = Path(path).read_bytes()

        result = runner.invoke(main, ["--config", path, "config", "migrate"])

        assert result.exit_code == 0, result.output
        assert "Migrated" in result.output
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["version"] == SCHEMA_VERSION
        assert all("color" in p for p in data["projects"])
        [backup] = config_io.backups_dir().glob("config-*.json")
        assert backup.read_bytes() == before

        # A subsequent load_config must still write nothing (R10 stays pure
        # even for a file migrate just persisted to).
        written = Path(path).read_bytes()
        load_config(path)
        assert Path(path).read_bytes() == written

    def test_a_current_file_is_left_alone(self, runner, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [{"path": "api", "color": "#111111"}],
            }
        )
        before = Path(path).read_bytes()
        result = runner.invoke(main, ["--config", path, "config", "migrate"])
        assert result.exit_code == 0, result.output
        assert "Already up to date" in result.output
        assert Path(path).read_bytes() == before
        assert not list(config_io.backups_dir().glob("config-*.json"))

    def test_a_missing_file_is_an_error(self, runner, tmp_path):
        absent = str(tmp_path / "absent.json")
        result = runner.invoke(main, ["--config", absent, "config", "migrate"])
        assert result.exit_code == 1
        assert "Config file not found" in result.output

    def test_text_with_no_utf8_form_is_refused_and_nothing_is_written(
        self, runner, tmp_config
    ):
        path = tmp_config({"projects": [{"path": "api", "title": "api\ud83d"}]})
        before = Path(path).read_bytes()
        result = runner.invoke(main, ["--config", path, "config", "migrate"])
        assert result.exit_code == 1
        assert "no UTF-8 form" in result.output
        assert Path(path).read_bytes() == before

    def test_a_migration_that_still_would_not_load_is_refused(self, runner, tmp_config):
        # The migrated text is validated like every other config write: a
        # config the loader refuses (an unknown node) is not stamped current.
        path = tmp_config(
            {"version": 3, "projects": [{"path": "api", "node": "nosuchnode"}]}
        )
        before = Path(path).read_bytes()
        result = runner.invoke(main, ["--config", path, "config", "migrate"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert Path(path).read_bytes() == before
        assert not list(config_io.backups_dir().glob("config-*.json"))


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


class TestMigrations:
    """Invariants of the migration chain itself, true at every schema version."""

    def test_every_version_below_the_schema_has_a_step(self):
        assert sorted(_MIGRATIONS) == list(range(SCHEMA_VERSION))


class TestMigrateToFour:
    """v4 adds the node pool (settings.nodes / nodeSync) and a project's
    node / push. Every new key is optional -- absent means "no nodes" -- so the
    step only stamps the version.

    Expected values are literals, never derived from the input: a shallow
    copy shares nested objects, so an expectation built from ``raw`` would
    move in lockstep with a step that mutated them."""

    def test_the_schema_is_four(self):
        assert SCHEMA_VERSION == 4

    def test_three_to_four_only_stamps(self):
        raw = {"version": 3, "settings": {"psmux": True}, "projects": [{"path": "a"}]}
        assert _migrate_3_to_4(raw) == {
            "version": 4,
            "settings": {"psmux": True},
            "projects": [{"path": "a"}],
        }

    def test_the_step_never_mutates_its_input(self):
        raw = {"version": 3, "settings": {"psmux": True}, "projects": [{"path": "a"}]}
        _migrate_3_to_4(raw)
        assert raw == {
            "version": 3,
            "settings": {"psmux": True},
            "projects": [{"path": "a"}],
        }

    def test_a_v3_config_reaches_four_with_its_projects_intact(self):
        raw = migrate_raw(
            {"version": 3, "projects": [{"path": "a", "windows": [{"name": "x"}]}]}
        )
        assert raw == {
            "version": 4,
            "projects": [{"path": "a", "windows": [{"name": "x"}]}],
        }

    def test_a_v3_file_loads_warns_and_is_left_alone(self, tmp_config, capsys):
        # The user-visible effect of this bump: an existing v3 config still
        # loads, says it is behind, and is never rewritten by a load.
        path = tmp_config({"version": 3, "projects": [{"path": "api"}]})
        before = Path(path).read_bytes()
        cfg = load_config(path)
        assert cfg.projects[0].path == "api"
        assert (
            "Warning: config schema v3 < v4; run: magent config migrate"
            in capsys.readouterr().err
        )
        assert Path(path).read_bytes() == before

    def test_an_unversioned_config_reaches_four(self):
        assert migrate_raw({"projects": []})["version"] == 4

    def test_migrating_a_v3_file_stamps_four_on_disk(self, runner, tmp_config):
        path = tmp_config(
            {"version": 3, "projects": [{"path": "api", "color": "#123456"}]}
        )
        result = runner.invoke(main, ["--config", path, "config", "migrate"])
        assert result.exit_code == 0, result.output
        assert json.loads(Path(path).read_text(encoding="utf-8"))["version"] == 4
