import json

from magent import cli
from magent.config import MagentConfig, ProjectConfig, Settings
from magent.launch import eligible_psmux_projects


def _cfg(projects, **settings):
    return MagentConfig(projects=projects, base_dir=None, settings=Settings(**settings))


class TestEligibleProjects:
    def test_filters_remote_ide_and_disabled(self):
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude"),
                ProjectConfig(path="/a/web", tool="codex"),
                ProjectConfig(path="/a/docs", tool="vscode"),
                ProjectConfig(path="/a/ide", tool="cursor"),
                ProjectConfig(path="/a/remote", tool="claude", host="me@box"),
                ProjectConfig(path="/a/off", tool="claude", enabled=False),
            ]
        )
        out = eligible_psmux_projects(cfg)
        assert [p["name"] for p in out] == ["api", "web"]
        assert out[0]["tool"] == "claude"
        assert out[0]["cmd"] == "claude --continue"
        assert out[1]["tool"] == "codex"

    def test_default_tool_applied(self):
        cfg = _cfg([ProjectConfig(path="/a/x")], default_tool="codex")
        out = eligible_psmux_projects(cfg)
        assert out[0]["tool"] == "codex"

    def test_name_is_display_and_session_is_sanitized(self):
        # P3-01: `name` is the raw display title (dots/spaces intact); `session`
        # is the psmux-sanitized socket id. A scripting consumer correlating
        # `up --json` against `status --json` joins on the display `name`.
        cfg = _cfg([ProjectConfig(path="/a/x", title="My App.1", tool="claude")])
        out = eligible_psmux_projects(cfg)
        assert out[0]["name"] == "My App.1"
        assert out[0]["session"] == "My-App-1"

    def test_group_filter_case_insensitive(self):
        cfg = _cfg(
            [
                ProjectConfig(path="/a/api", tool="claude", group="INTERNAL"),
                ProjectConfig(path="/a/web", tool="claude", group="LEAD"),
                ProjectConfig(path="/a/x", tool="claude"),
            ]
        )
        out = eligible_psmux_projects(cfg, group="internal")
        assert [p["name"] for p in out] == ["api"]

    def test_group_filter_no_match(self):
        cfg = _cfg([ProjectConfig(path="/a/api", tool="claude", group="INTERNAL")])
        assert eligible_psmux_projects(cfg, group="NOPE") == []


class TestGroupedOverview:
    def test_grouped_preserves_order(self):
        order, buckets = cli._grouped(
            [
                {"name": "a", "group": "X"},
                {"name": "b", "group": "Y"},
                {"name": "c", "group": "X"},
                {"name": "d"},
            ]
        )
        assert order == ["X", "Y", "(no group)"]
        assert buckets["X"] == ["a", "c"]
        assert buckets["(no group)"] == ["d"]

    def test_overview_pickable_excludes_no_group(self, capsys):
        up = [{"name": "z", "group": "AUTOMATIONS"}]
        down = [
            {"name": "a", "group": "INTERNAL"},
            {"name": "b", "group": "LEAD"},
            {"name": "c"},
        ]
        pickable = cli._print_session_overview("host", up, down)
        assert pickable == ["INTERNAL", "LEAD"]
        out = capsys.readouterr().out
        assert "INTERNAL" in out and "LEAD" in out and "AUTOMATIONS" in out


class TestDefaultAttachHost:
    def test_picks_most_common_host(self, tmp_path, monkeypatch):
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(
            json.dumps(
                {
                    "projects": [
                        {"path": "a", "host": "u@h1"},
                        {"path": "b", "host": "u@h1"},
                        {"path": "c", "host": "u@h2"},
                        {"path": "d"},
                    ]
                }
            )
        )
        monkeypatch.setattr("magent.cli.attach.find_config", lambda *_: cfgfile)
        assert cli._default_attach_host() == "u@h1"

    def test_none_when_no_hosts(self, tmp_path, monkeypatch):
        cfgfile = tmp_path / "c.json"
        cfgfile.write_text(json.dumps({"projects": [{"path": "a"}]}))
        monkeypatch.setattr("magent.cli.attach.find_config", lambda *_: cfgfile)
        assert cli._default_attach_host() is None


class TestSplitTarget:
    def test_with_user(self):
        assert cli._split_target("amin@host.ts.net") == ("amin", "host.ts.net")

    def test_without_user(self):
        user, hostname = cli._split_target("host.ts.net")
        assert hostname == "host.ts.net"
        assert user  # current user, non-empty


class TestSshJsonParsing:
    def test_skips_banner_lines(self, monkeypatch):
        noisy = 'WARNING: banner\nMOTD line\n{"up": [], "down": []}\n'
        monkeypatch.setattr(
            "magent.cli.attach._ssh_capture", lambda *a, **k: (0, noisy, "")
        )
        assert cli._ssh_json("u@h", "magent up --json") == {"up": [], "down": []}

    def test_returns_none_without_json(self, monkeypatch):
        monkeypatch.setattr(
            "magent.cli.attach._ssh_capture",
            lambda *a, **k: (255, "no route to host", "err"),
        )
        assert cli._ssh_json("u@h", "magent up --json") is None


class TestAttachNomux:
    """NF-S3-004 + P4: first coverage of _attach_nomux. Its fallback command
    derives from the tool registry (DEFAULT_TOOLS['claude']), never a
    hard-coded literal that could silently drift from the default."""

    def _run(self, monkeypatch, projects):
        from magent.cli import attach as attach_mod

        calls: list[list[str]] = []
        monkeypatch.setattr(
            attach_mod.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd)
        )
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda *a, **k: None)
        attach_mod._attach_nomux("u@host", {"projects": projects})
        return calls

    def test_fallback_cmd_derived_from_default_tools(self, monkeypatch):
        from magent.config import DEFAULT_TOOLS

        calls = self._run(monkeypatch, [{"path": "api", "name": "api"}])
        # The remote command is the last Popen argument: `cd <dir> && <cmd>`.
        assert calls[0][-1] == f"cd api && {DEFAULT_TOOLS['claude']}"

    def test_uses_explicit_cmd_when_present(self, monkeypatch):
        calls = self._run(monkeypatch, [{"path": "web", "name": "web", "cmd": "codex"}])
        assert calls[0][-1] == "cd web && codex"


class TestUpJsonConfigError:
    """NF-S3-005: up --json emits a JSON error envelope (not a stderr line) on
    an invalid config, now that up_cmd routes through _load_config_or_exit."""

    def test_invalid_config_emits_json_error_exit_1(self, runner, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json")
        result = runner.invoke(cli.main, ["--config", str(bad), "up", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]
