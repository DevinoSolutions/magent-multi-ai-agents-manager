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


class TestQueryStatusDiagnostics:
    """The status read reports WHY it failed (timeout / ssh error / missing
    magent) instead of one generic line, and retries once with a longer
    timeout when the host is slow to answer (e.g. just booted)."""

    def test_timeout_retries_with_longer_timeout(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        timeouts: list[int] = []

        def fake_capture(target, cmd, timeout=30):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                return (124, "", "ssh timed out")
            return (0, '{"ok": true, "up": []}', "")

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        status, rc, _ = attach_mod._query_status("u@h", "")
        assert status == {"ok": True, "up": []}
        assert rc == 0
        assert timeouts == [
            attach_mod._STATUS_TIMEOUT_S,
            attach_mod._STATUS_RETRY_TIMEOUT_S,
        ]
        assert "retrying" in capsys.readouterr().out

    def test_double_timeout_explains_slow_host(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(
            attach_mod, "_ssh_capture", lambda *a, **k: (124, "", "ssh timed out")
        )
        status, rc, err = attach_mod._query_status("u@h", "")
        assert status is None
        attach_mod._explain_status_failure("u@h", rc, err)
        out = capsys.readouterr().out
        assert "Could not read project status" in out
        assert "timed out" in out

    def test_ssh_error_shows_stderr_detail(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(
            attach_mod,
            "_ssh_capture",
            lambda *a, **k: (255, "", "Permission denied (publickey)."),
        )
        status, rc, err = attach_mod._query_status("u@h", "")
        assert status is None
        attach_mod._explain_status_failure("u@h", rc, err)
        out = capsys.readouterr().out
        assert "ssh exited 255" in out
        assert "Permission denied" in out

    def test_command_missing_hints_install(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(
            attach_mod,
            "_ssh_capture",
            lambda *a, **k: (1, "", "'magent' is not recognized as an internal..."),
        )
        status, rc, err = attach_mod._query_status("u@h", "")
        assert status is None
        attach_mod._explain_status_failure("u@h", rc, err)
        assert "Is magent installed" in capsys.readouterr().out

    def test_clean_run_without_json_probes_version(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        def fake_capture(target, cmd, timeout=30):
            if "--version" in cmd:
                return (1, "", "")
            return (0, "interactive wizard text, no JSON", "")

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        status, rc, err = attach_mod._query_status("u@h", "")
        assert status is None
        attach_mod._explain_status_failure("u@h", rc, err)
        assert "Is magent installed" in capsys.readouterr().out


class TestBringUpAndRequery:
    """The bring-up requery polls until the host's up-count stabilizes: on a
    loaded host, sessions keep materializing after `magent up` returns (or
    times out), and a single snapshot opened windows onto a partial list."""

    def _run(self, monkeypatch, snapshots, up_rc=0):
        from magent.cli import attach as attach_mod

        polls = iter(snapshots)

        def fake_capture(target, cmd, timeout=30):
            assert timeout == attach_mod._BRING_UP_TIMEOUT_S
            return (up_rc, "", "" if up_rc == 0 else "ssh timed out")

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        monkeypatch.setattr(
            attach_mod,
            "_ssh_json",
            lambda *a, **k: {"up": [{"name": f"s{i}"} for i in range(next(polls))]},
        )
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        return attach_mod._bring_up_and_requery("u@h", "", [])

    def test_waits_for_count_to_stabilize(self, monkeypatch):
        # Growing 2 -> 5 -> 39, then stable: the final list wins.
        result = self._run(monkeypatch, [2, 5, 39, 39])
        assert len(result) == 39

    def test_timeout_still_polls_for_sessions(self, monkeypatch, capsys):
        result = self._run(monkeypatch, [10, 10], up_rc=124)
        assert len(result) == 10
        assert "bring-up exited 124" in capsys.readouterr().out

    def test_unreachable_requery_returns_fallback(self, monkeypatch):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        monkeypatch.setattr(attach_mod, "_ssh_json", lambda *a, **k: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        fallback = [{"name": "kept"}]
        assert attach_mod._bring_up_and_requery("u@h", "", fallback) == fallback


class TestLocalGrid:
    def test_reads_local_layout(self, tmp_path, monkeypatch):
        from magent.cli import attach as attach_mod

        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"layout": {"columns": 3, "rows": 2}}))
        monkeypatch.setattr("magent.cli.attach.find_config", lambda *_: cfg)
        assert attach_mod._local_grid() == (3, 2)

    def test_defaults_without_config(self, tmp_path, monkeypatch):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(
            "magent.cli.attach.find_config", lambda *_: tmp_path / "missing.json"
        )
        assert attach_mod._local_grid() == (2, 1)

    def test_garbage_layout_defaults(self, tmp_path, monkeypatch):
        from magent.cli import attach as attach_mod

        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({"layout": {"columns": 0, "rows": "x"}}))
        monkeypatch.setattr("magent.cli.attach.find_config", lambda *_: cfg)
        assert attach_mod._local_grid() == (1, 1)


class TestAttachWindowCommand:
    def test_remote_command_attaches_directly_with_picker_fallback(self, monkeypatch):
        # Direct psmux attach is the fast path (no python boot per window);
        # the full magent picker only runs when that fails.
        from magent.cli import attach as attach_mod

        status = {
            "up": [{"name": "myapp", "session": "myapp"}],
            "down": [],
            "projects": [{"name": "myapp"}],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        popen_calls: list[list[str]] = []
        monkeypatch.setattr(
            attach_mod.subprocess, "Popen", lambda args, **k: popen_calls.append(args)
        )
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)

        class _NoHotkey:
            def supports_hotkey(self) -> bool:
                return False

        monkeypatch.setattr("magent.platform.get_platform", _NoHotkey)

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        assert popen_calls[0][-1] == "psmux -L myapp attach || magent sessions myapp"


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
