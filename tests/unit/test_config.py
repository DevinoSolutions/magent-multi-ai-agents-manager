from pathlib import Path

import pytest

from magent.config import SCHEMA_VERSION, ConfigError, MagentConfig, load_config
from magent.init_config import generate_config, scan_for_projects


class TestLoadConfig:
    def test_minimal_valid_config(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api"}]})
        cfg = load_config(path)
        assert isinstance(cfg, MagentConfig)
        assert len(cfg.projects) == 1
        assert cfg.projects[0].path == "api"

    def test_full_config(self, tmp_config):
        path = tmp_config(
            {
                "baseDir": "C:/code",
                "layout": {"columns": 3, "rows": 2},
                "settings": {
                    "defaultTool": "codex",
                    "settleSeconds": 5,
                    "launchDelayMs": 200,
                    "ssh": {"shell": "zsh -lc"},
                    "tools": {"claude": "claude --continue", "codex": "codex --yolo"},
                },
                "projects": [
                    {
                        "path": "api",
                        "group": "backend",
                        "color": "#ff0000",
                        "tool": "claude",
                        "title": "my-api",
                        "enabled": True,
                        "host": None,
                        "remotePath": None,
                        "windows": 3,
                    },
                ],
            }
        )
        cfg = load_config(path)
        assert cfg.base_dir == "C:/code"
        assert cfg.layout.columns == 3
        assert cfg.layout.rows == 2
        assert cfg.settings.default_tool == "codex"
        assert cfg.settings.settle_seconds == 5
        assert cfg.settings.launch_delay_ms == 200
        assert cfg.settings.ssh.shell == "zsh -lc"
        assert cfg.settings.tools["codex"] == "codex --yolo"
        p = cfg.projects[0]
        assert p.group == "backend"
        assert p.color == "#ff0000"
        assert p.windows is not None
        assert len(p.windows) == 3

    def test_defaults_applied(self, tmp_config):
        path = tmp_config({"projects": [{"path": "x"}]})
        cfg = load_config(path)
        assert cfg.base_dir is None
        assert cfg.layout.columns == 2
        assert cfg.layout.rows == 1
        assert cfg.settings.default_tool == "claude"
        assert cfg.settings.settle_seconds == 3
        assert cfg.settings.launch_delay_ms == 400
        assert cfg.settings.ssh.shell == "bash -lc"
        assert "claude" in cfg.settings.tools

    def test_windows_as_string_array(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api", "windows": ["feat", "bugs"]}]})
        cfg = load_config(path)
        assert cfg.projects[0].windows is not None
        assert [w.name for w in cfg.projects[0].windows] == ["feat", "bugs"]

    def test_windows_omitted_is_none(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api"}]})
        cfg = load_config(path)
        assert cfg.projects[0].windows is None

    def test_enabled_defaults_true(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api"}]})
        cfg = load_config(path)
        assert cfg.projects[0].enabled is True

    def test_enabled_false(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api", "enabled": False}]})
        cfg = load_config(path)
        assert cfg.projects[0].enabled is False

    def test_missing_projects_raises(self, tmp_config):
        path = tmp_config({"layout": {"columns": 2}})
        with pytest.raises(ValueError, match="projects"):
            load_config(path)

    def test_project_missing_path_raises(self, tmp_config):
        path = tmp_config({"projects": [{"group": "x"}]})
        with pytest.raises(ValueError, match="path"):
            load_config(path)

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json{{{")
        with pytest.raises(ValueError, match="valid JSON"):
            load_config(str(p))

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.json")

    def test_happy_defaults_false(self, tmp_config):
        path = tmp_config({"projects": [{"path": "x"}]})
        cfg = load_config(path)
        assert cfg.settings.happy is False

    def test_happy_enabled_globally(self, tmp_config):
        path = tmp_config(
            {
                "settings": {"happy": True},
                "projects": [{"path": "x"}],
            }
        )
        cfg = load_config(path)
        assert cfg.settings.happy is True

    def test_happy_per_project(self, tmp_config):
        path = tmp_config(
            {
                "settings": {"happy": False},
                "projects": [
                    {"path": "a", "happy": True},
                    {"path": "b"},
                    {"path": "c", "happy": False},
                ],
            }
        )
        cfg = load_config(path)
        assert cfg.projects[0].happy is True
        assert cfg.projects[1].happy is None
        assert cfg.projects[2].happy is False

    def test_psmux_defaults_false(self, tmp_config):
        path = tmp_config({"projects": [{"path": "x"}]})
        cfg = load_config(path)
        assert cfg.settings.psmux is False

    def test_psmux_enabled(self, tmp_config):
        path = tmp_config(
            {
                "settings": {"psmux": True},
                "projects": [{"path": "x"}],
            }
        )
        cfg = load_config(path)
        assert cfg.settings.psmux is True

    def test_load_config_backfills_colors_without_writing_file(self, tmp_config):
        # F-D6-003/007: load_config backfills a missing color IN MEMORY so
        # callers always see cfg.projects[*].color populated, but load must
        # never write to disk as a side effect (R10) -- persistence is
        # `magent config migrate`'s job now.
        path = tmp_config({"projects": [{"path": "api"}]})
        before = Path(path).read_bytes()
        cfg = load_config(path)
        after = Path(path).read_bytes()
        assert cfg.projects[0].color is not None
        assert before == after


class TestDeterministicColors:
    """P3-07: a colorless project gets a DETERMINISTIC color derived from a
    stable hash of its title/path -- the same color every load, before any
    `config migrate` persists it (no more per-load randomness)."""

    def test_derive_is_deterministic(self):
        from magent.config import _derive_tab_color

        a = _derive_tab_color("api", set())
        b = _derive_tab_color("api", set())
        assert a == b
        assert a.startswith("#") and len(a) == 7

    def test_distinct_identities_differ(self):
        from magent.config import _derive_tab_color

        assert _derive_tab_color("api", set()) != _derive_tab_color("web", set())

    def test_avoids_collision_within_config(self):
        from magent.config import _derive_tab_color

        first = _derive_tab_color("api", set())
        second = _derive_tab_color("api", {first})
        assert second != first
        assert second.startswith("#") and len(second) == 7

    def test_backfill_is_stable_across_loads(self, tmp_config):
        path = tmp_config({"projects": [{"path": "api"}, {"path": "web"}]})
        first = [p.color for p in load_config(path).projects]
        second = [p.color for p in load_config(path).projects]
        assert first == second
        assert all(c is not None for c in first)

    def test_load_config_drops_unknown_settings_key(self, capsys, tmp_config):
        # F-D6-004: unknown settings keys are still dropped from the parsed
        # Settings object (there's nowhere to put them), but now surface a
        # stderr warning (R10) instead of vanishing silently.
        path = tmp_config(
            {
                "settings": {"bogusKey": 1, "defaultTool": "codex"},
                "projects": [{"path": "api"}],
            }
        )
        cfg = load_config(path)
        assert not hasattr(cfg.settings, "bogusKey")
        assert cfg.settings.default_tool == "codex"
        assert "bogusKey" in capsys.readouterr().err

    def test_load_config_wrong_typed_columns_raises(self, tmp_config):
        # F-D6-005: wrong-typed layout.columns now raises a clean ConfigError
        # (was a raw TypeError out of max(1, "2")).
        path = tmp_config(
            {
                "layout": {"columns": "2"},
                "projects": [{"path": "api"}],
            }
        )
        with pytest.raises(ConfigError, match=r"layout\.columns must be an integer"):
            load_config(path)

    def test_load_config_bool_columns_raises(self, tmp_config):
        # bool is an int subclass in Python -- _require_type must reject it
        # for an int-only field rather than silently accepting True/False.
        path = tmp_config(
            {
                "layout": {"columns": True},
                "projects": [{"path": "api"}],
            }
        )
        with pytest.raises(ConfigError, match=r"layout\.columns must be an integer"):
            load_config(path)

    def test_missing_version_defaults_zero_and_warns(self, capsys, tmp_config):
        # R10: a config file with no top-level "version" loads as legacy v0
        # and nudges the user toward `magent config migrate`.
        path = tmp_config({"projects": [{"path": "api"}]})
        cfg = load_config(path)
        assert cfg.version == 0
        assert "migrate" in capsys.readouterr().err

    def test_version_at_current_schema_warns_nothing(self, capsys, tmp_config):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        cfg = load_config(path)
        assert cfg.version == SCHEMA_VERSION
        assert capsys.readouterr().err == ""


class TestAttentionSettings:
    def test_defaults_when_absent(self, tmp_config):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        att = load_config(path).settings.attention
        assert (att.badge, att.flash, att.toast, att.ntfy) == (
            True,
            True,
            False,
            False,
        )

    def test_explicit_values_parse(self, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"attention": {"badge": False, "toast": True}},
                "projects": [{"path": "api"}],
            }
        )
        att = load_config(path).settings.attention
        assert att.badge is False
        assert att.flash is True  # unspecified keys keep their defaults
        assert att.toast is True

    def test_unknown_attention_key_warns(self, capsys, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"attention": {"bogus": True}},
                "projects": [{"path": "api"}],
            }
        )
        load_config(path)
        assert "settings.attention.bogus" in capsys.readouterr().err

    def test_notify_on_done_defaults_off(self, tmp_config):
        # Opt-in: an absent notifyOnDone parses to False (push-on-done is off).
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        assert load_config(path).settings.attention.notify_on_done is False

    def test_notify_on_done_parses(self, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"attention": {"notifyOnDone": True}},
                "projects": [{"path": "api"}],
            }
        )
        assert load_config(path).settings.attention.notify_on_done is True


class TestWindowTitlePrefix:
    def test_defaults_on_when_absent(self, tmp_config):
        # Additive field with a safe default: an absent windowTitlePrefix parses
        # to True (the magent: prefix is on), so existing configs are unchanged.
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        assert load_config(path).settings.window_title_prefix is True

    def test_explicit_false_parses(self, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"windowTitlePrefix": False},
                "projects": [{"path": "api"}],
            }
        )
        assert load_config(path).settings.window_title_prefix is False

    def test_is_a_known_settings_key(self, capsys, tmp_config):
        # windowTitlePrefix must be in _ALLOWED_SETTINGS_KEYS -- otherwise
        # load_config would warn "unknown config key" on every load.
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"windowTitlePrefix": True},
                "projects": [{"path": "api"}],
            }
        )
        load_config(path)
        assert capsys.readouterr().err == ""


class TestPathResolution:
    def test_resolve_relative(self, tmp_config):
        path = tmp_config(
            {
                "baseDir": "/home/user/code",
                "projects": [{"path": "api"}],
            }
        )
        cfg = load_config(path)
        assert cfg.projects[0].path == "api"

    def test_resolve_absolute(self, tmp_config):
        path = tmp_config({"projects": [{"path": "/absolute/path"}]})
        cfg = load_config(path)
        assert cfg.projects[0].path == "/absolute/path"


class TestScanForProjects:
    def test_finds_git_repos(self, tmp_path):
        (tmp_path / "api" / ".git").mkdir(parents=True)
        (tmp_path / "web" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        paths = [r["path"] for r in repos]
        assert "api" in paths
        assert "web" in paths

    def test_finds_nested_repos(self, tmp_path):
        (tmp_path / "internal" / "api" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        assert any(r["path"] == "internal/api" for r in repos)

    def test_adds_group_from_parent_folder(self, tmp_path):
        (tmp_path / "backend" / "api" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        proj = next(r for r in repos if r["path"] == "backend/api")
        assert proj["group"] == "backend"

    def test_no_group_for_top_level(self, tmp_path):
        (tmp_path / "api" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        proj = next(r for r in repos if r["path"] == "api")
        assert "group" not in proj

    def test_duplicate_leaf_gets_unique_title(self, tmp_path):
        (tmp_path / "frontend" / "api" / ".git").mkdir(parents=True)
        (tmp_path / "backend" / "api" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        api_repos = [r for r in repos if r["path"].endswith("api")]
        titles = [r.get("title") for r in api_repos]
        assert all(t is not None for t in titles)
        assert len(set(titles)) == 2

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "node_modules" / "pkg" / ".git").mkdir(parents=True)
        (tmp_path / "api" / ".git").mkdir(parents=True)
        repos = scan_for_projects(str(tmp_path))
        assert len(repos) == 1

    def test_fallback_to_subdirectories(self, tmp_path):
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        repos = scan_for_projects(str(tmp_path))
        assert len(repos) == 2

    def test_records_skipped_unreadable_dirs(self, tmp_path, monkeypatch):
        # A subdir whose iterdir() is denied is skipped, and its path is
        # recorded so the CLI can report the omission once (P2-06).
        (tmp_path / "locked").mkdir()
        (tmp_path / "ok" / ".git").mkdir(parents=True)
        real_iterdir = Path.iterdir

        def fake_iterdir(self):
            if self.name == "locked":
                raise PermissionError("denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", fake_iterdir)
        skipped: list[str] = []
        scan_for_projects(str(tmp_path), skipped=skipped)
        assert len(skipped) == 1
        assert skipped[0].endswith("locked")


class TestGenerateConfig:
    def test_generates_valid_config(self, tmp_path):
        (tmp_path / "api" / ".git").mkdir(parents=True)
        (tmp_path / "web" / ".git").mkdir(parents=True)
        config = generate_config(str(tmp_path))
        assert config["baseDir"] == str(tmp_path).replace("\\", "/")
        assert len(config["projects"]) == 2
        assert config["layout"]["columns"] == 2
        assert config["settings"]["defaultTool"] == "claude"
