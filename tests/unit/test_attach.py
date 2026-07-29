import json

from magent import cli
from magent.config import SCHEMA_VERSION, MagentConfig, ProjectConfig, Settings
from magent.launch import eligible_psmux_projects


def _cfg(projects, **settings):
    return MagentConfig(projects=projects, base_dir=None, settings=Settings(**settings))


class _FakeProc:
    """Stand-in for a subprocess.Popen handle (the attach flow waits on the
    overlapped `serve --ensure` hop, so a bare None no longer suffices)."""

    def __init__(self, rc: int = 0) -> None:
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int:
        return self._rc

    def kill(self) -> None:  # pragma: no cover - only on the timeout path
        pass


class _AttachPlatform:
    """The slice of Platform the attach flow reaches for: the open-window
    snapshot it consults before spawning, plus the hotkey capability gate."""

    def __init__(self, windows: dict[str, int] | None = None) -> None:
        self._windows = windows or {}

    def snapshot_windows(self) -> dict[str, int]:
        return self._windows

    def supports_hotkey(self) -> bool:
        return False


def _fake_platform(monkeypatch, windows: dict[str, int] | None = None) -> None:
    monkeypatch.setattr(
        "magent.platform.get_platform", lambda: _AttachPlatform(windows)
    )


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


class TestLastAttachHost:
    """`magent attach` remembers the last successfully attached target and
    offers it as the prompt default on the next no-argument run, preferring
    it over the config-derived guess."""

    def _isolate(self, monkeypatch, tmp_path):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(attach_mod, "_LAST_HOST_FILE", tmp_path / "last-host")
        return attach_mod

    def test_roundtrip(self, monkeypatch, tmp_path):
        attach_mod = self._isolate(monkeypatch, tmp_path)
        assert attach_mod._read_last_host() is None
        attach_mod._remember_last_host("amin@desktop.ts.net")
        assert attach_mod._read_last_host() == "amin@desktop.ts.net"

    def test_blank_file_reads_as_none(self, monkeypatch, tmp_path):
        attach_mod = self._isolate(monkeypatch, tmp_path)
        attach_mod._LAST_HOST_FILE.write_text("  \n", encoding="utf-8")
        assert attach_mod._read_last_host() is None

    def test_remember_survives_unwritable_dir(self, monkeypatch, tmp_path):
        from magent.cli import attach as attach_mod

        blocked = tmp_path / "not-a-dir"
        blocked.write_text("file, not dir", encoding="utf-8")
        monkeypatch.setattr(attach_mod, "_LAST_HOST_FILE", blocked / "last-host")
        attach_mod._remember_last_host("u@h")  # must not raise

    def test_prompt_prefers_last_host_over_config(self, monkeypatch, tmp_path):
        import click

        attach_mod = self._isolate(monkeypatch, tmp_path)
        attach_mod._remember_last_host("amin@last-used")
        monkeypatch.setattr(attach_mod, "_default_attach_host", lambda: "amin@config")
        seen: dict[str, object] = {}

        def fake_prompt(text, **kwargs):
            seen.update(kwargs)
            return ""

        monkeypatch.setattr(click, "prompt", fake_prompt)
        import pytest

        with pytest.raises(SystemExit):
            attach_mod._attach_flow(None, no_mux=False, group=None, yes=False)
        assert seen["default"] == "amin@last-used"

    def test_successful_status_read_remembers_target(self, monkeypatch, tmp_path):
        import json as json_mod
        import subprocess as subprocess_mod
        import time as time_mod

        attach_mod = self._isolate(monkeypatch, tmp_path)
        status = {
            "up": [{"name": "myapp"}],
            "down": [],
            "projects": [{"name": "myapp", "path": "myapp"}],
        }
        monkeypatch.setattr(
            attach_mod, "_ssh_capture", lambda *a, **k: (0, json_mod.dumps(status), "")
        )
        monkeypatch.setattr(subprocess_mod, "Popen", lambda *a, **k: _FakeProc())
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(time_mod, "sleep", lambda s: None)
        _fake_platform(monkeypatch)

        attach_mod._attach_flow("someone@box", no_mux=False, group=None, yes=False)
        assert attach_mod._read_last_host() == "someone@box"


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

    def _run(self, monkeypatch, snapshots, up_rc=0, expected=10**6, track=None):
        from magent.cli import attach as attach_mod

        polls = iter(snapshots)

        def fake_capture(target, cmd, timeout=30):
            assert timeout == attach_mod._BRING_UP_TIMEOUT_S
            return (up_rc, "", "" if up_rc == 0 else "ssh timed out")

        def fake_json(*a, **k):
            n = next(polls)
            if track is not None:
                track["queries"].append(n)
            return {"up": [{"name": f"s{i}"} for i in range(n)]}

        def fake_sleep(s):
            if track is not None:
                track["sleeps"].append(s)

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        monkeypatch.setattr(attach_mod, "_ssh_json", fake_json)
        monkeypatch.setattr(attach_mod.time, "sleep", fake_sleep)
        return attach_mod._bring_up_and_requery("u@h", "", [], expected)

    def test_waits_for_count_to_stabilize(self, monkeypatch):
        # Growing 2 -> 5 -> 39, then stable: the final list wins.
        result = self._run(monkeypatch, [2, 5, 39, 39])
        assert len(result) == 39

    def test_timeout_still_polls_for_sessions(self, monkeypatch, capsys):
        result = self._run(monkeypatch, [10, 10], up_rc=124)
        assert len(result) == 10
        assert "bring-up exited 124" in capsys.readouterr().out

    def test_expected_count_on_first_poll_exits_immediately(self, monkeypatch):
        # The common path: everything came up. One status query, no sleep --
        # stall detection has nothing left to detect, and waiting out an
        # interval to prove it cost ~10s on every attach.
        track = {"queries": [], "sleeps": []}
        result = self._run(monkeypatch, [7, 7, 7], expected=7, track=track)
        assert len(result) == 7
        assert track["queries"] == [7]
        assert track["sleeps"] == []

    def test_short_of_expected_falls_back_to_stall_detection(self, monkeypatch):
        # A session that never comes up: 4 of an expected 5. Two equal readings
        # end the poll, and the interval sleep between them still happens.
        from magent.cli import attach as attach_mod

        track = {"queries": [], "sleeps": []}
        result = self._run(monkeypatch, [4, 4, 4], expected=5, track=track)
        assert len(result) == 4
        assert track["queries"] == [4, 4]
        assert track["sleeps"] == [attach_mod._STABILIZE_INTERVAL_S]

    def test_unreachable_requery_returns_fallback(self, monkeypatch):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        monkeypatch.setattr(attach_mod, "_ssh_json", lambda *a, **k: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        fallback = [{"name": "kept"}]
        assert attach_mod._bring_up_and_requery("u@h", "", fallback, 5) == fallback


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

        def fake_popen(args, **k):
            popen_calls.append(args)
            return _FakeProc()

        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        _fake_platform(monkeypatch)

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        assert popen_calls[0][-1] == "psmux -L myapp attach || magent sessions myapp"


class TestAttachSkipsOpenWindows:
    """Re-running attach while the previous attach's windows are still open
    must not stack a second window on the same psmux session. The check reuses
    tiling.window_open, so a badged title still counts as open -- and an
    already-open window is still handed to tiling so it lands in the grid."""

    def _run_flow(self, monkeypatch, sessions, windows):
        from magent.cli import attach as attach_mod

        status = {
            "up": [{"name": s, "session": s} for s in sessions],
            "down": [],
            "projects": [{"name": s} for s in sessions],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        spawns: list[list[str]] = []

        def fake_popen(args, **k):
            # The `serve --ensure` hop is not a window spawn; only count `wt`.
            if args and args[0] == "wt":
                spawns.append(args)
            return _FakeProc()

        sleeps: list[float] = []
        tiled: list[list[str]] = []
        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", tiled.append)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", sleeps.append)
        # Keep the real ~/.magent/last-attach-host out of a unit run.
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        _fake_platform(monkeypatch, windows)

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        return spawns, sleeps, tiled[0]

    def test_open_session_is_not_respawned_but_is_still_tiled(self, monkeypatch):
        spawns, sleeps, titles = self._run_flow(monkeypatch, ["api"], {"magent:api": 1})
        assert spawns == []
        assert sleeps == []  # no stagger for a window we did not spawn
        assert titles == ["magent:api"]

    def test_badged_title_still_counts_as_open(self, monkeypatch):
        # titles.make_title(name, "needs-input") -> "magent:[!] api"
        spawns, _, titles = self._run_flow(monkeypatch, ["api"], {"magent:[!] api": 1})
        assert spawns == []
        assert titles == ["magent:api"]

    def test_mixed_spawns_only_the_missing_one_and_tiles_both(self, monkeypatch):
        spawns, sleeps, titles = self._run_flow(
            monkeypatch, ["api", "web"], {"magent:api": 1}
        )
        assert len(spawns) == 1
        assert spawns[0][-1] == "psmux -L web attach || magent sessions web"
        assert len(sleeps) == 1  # stagger paid once, for the one real spawn
        assert titles == ["magent:api", "magent:web"]

    def test_all_missing_spawns_every_window(self, monkeypatch):
        spawns, sleeps, titles = self._run_flow(monkeypatch, ["api", "web"], {})
        assert len(spawns) == 2
        assert len(sleeps) == 2
        assert titles == ["magent:api", "magent:web"]


class TestAttachNomux:
    """NF-S3-004 + P4: first coverage of _attach_nomux. Its fallback command
    derives from the tool registry (DEFAULT_TOOLS['claude']), never a
    hard-coded literal that could silently drift from the default."""

    def _run(self, monkeypatch, projects, windows=None):
        from magent.cli import attach as attach_mod

        calls: list[list[str]] = []
        sleeps: list[float] = []
        tiled: list[list[str]] = []
        monkeypatch.setattr(
            attach_mod.subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd)
        )
        monkeypatch.setattr(attach_mod, "_tile_titles", tiled.append)
        monkeypatch.setattr(attach_mod.time, "sleep", sleeps.append)
        _fake_platform(monkeypatch, windows)
        attach_mod._attach_nomux("u@host", {"projects": projects})
        return calls, sleeps, tiled[0]

    def test_fallback_cmd_derived_from_default_tools(self, monkeypatch):
        from magent.config import DEFAULT_TOOLS

        calls, _, _ = self._run(monkeypatch, [{"path": "api", "name": "api"}])
        # The remote command is the last Popen argument: `cd <dir> && <cmd>`.
        assert calls[0][-1] == f"cd api && {DEFAULT_TOOLS['claude']}"

    def test_uses_explicit_cmd_when_present(self, monkeypatch):
        calls, _, _ = self._run(
            monkeypatch, [{"path": "web", "name": "web", "cmd": "codex"}]
        )
        assert calls[0][-1] == "cd web && codex"

    def test_open_window_is_not_respawned_but_is_still_tiled(self, monkeypatch):
        calls, sleeps, titles = self._run(
            monkeypatch,
            [{"path": "api", "name": "api"}, {"path": "web", "name": "web"}],
            {"magent:[+] api": 1},
        )
        assert len(calls) == 1
        assert calls[0][-1].startswith("cd web && ")
        assert len(sleeps) == 1
        assert titles == ["magent:api", "magent:web"]


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


# The `projects` shape psmux_status returns (up --json serializes every key).
_PROJECT_ROWS = [
    {
        "name": "api",
        "session": "api",
        "path": "/a/api",
        "tool": "claude",
        "group": None,
        "resolved": "/a/api",
        "cmd": "claude --continue",
    }
]


class TestUpRevive:
    """`up` re-launches the agent in sessions that are alive but parked at a
    bare shell. Interactive runs always do it; `--json` stays a pure read
    unless --revive is passed, because attach polls it repeatedly."""

    def _config(self, tmp_path):
        p = tmp_path / "magent.config.json"
        p.write_text(
            json.dumps(
                {
                    "version": SCHEMA_VERSION,
                    "projects": [{"path": "/a/api", "tool": "claude"}],
                    "settings": {"uploadServer": False},
                }
            )
        )
        return str(p)

    def _patch(self, monkeypatch, revived, up=None):
        """Stub the two heavy subsystem calls up_cmd makes; record revive args."""
        calls: list[dict[str, object]] = []
        rows = up if up is not None else [{"name": "api", "session": "api"}]
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (rows, [], _PROJECT_ROWS),
        )

        def fake_revive(cfg, only=None, group=None):
            calls.append({"only": only, "group": group})
            return revived

        monkeypatch.setattr("magent.launch.revive_psmux", fake_revive)
        return calls

    def test_json_is_a_pure_read_by_default(self, runner, tmp_path, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("plain `up --json` must not revive anything")

        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"name": "api", "session": "api"}],
                [],
                _PROJECT_ROWS,
            ),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", _fail)

        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        # The key is always present so a consumer can read it unconditionally.
        assert payload["revived"] == []

    def test_json_revive_flag_revives_the_live_sessions(
        self, runner, tmp_path, monkeypatch
    ):
        calls = self._patch(monkeypatch, revived=["api"])
        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json", "--revive"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["revived"] == ["api"]
        # Only sessions that were ALREADY up are candidates -- a session just
        # created by bring-up may still be booting its agent.
        assert calls == [{"only": ["api"], "group": None}]

    def test_interactive_up_revives_without_the_flag(
        self, runner, tmp_path, monkeypatch
    ):
        self._patch(monkeypatch, revived=["api", "web"])
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert "Revived agent in" in result.output
        assert "api, web" in result.output

    def test_interactive_up_stays_quiet_when_nothing_was_dead(
        self, runner, tmp_path, monkeypatch
    ):
        self._patch(monkeypatch, revived=[])
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert "Revived" not in result.output


class TestQueryStatusRevives:
    def test_remote_command_revives_with_legacy_fallback(self, monkeypatch):
        # A host on an older magent rejects the unknown --revive flag and prints
        # nothing on stdout; `||` falls back to the plain read so attach still
        # works (same trick as `psmux attach || magent sessions`).
        from magent.cli import attach as attach_mod

        seen: list[str] = []

        def fake_capture(target, cmd, timeout=30):
            seen.append(cmd)
            return (0, '{"ok": true, "up": []}', "")

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        attach_mod._query_status("u@h", ' -g "core"')

        assert seen == [
            'magent up --json --revive -g "core" || magent up --json -g "core"'
        ]

    def test_retry_reuses_the_same_command(self, monkeypatch):
        from magent.cli import attach as attach_mod

        seen: list[str] = []

        def fake_capture(target, cmd, timeout=30):
            seen.append(cmd)
            if len(seen) == 1:
                return (124, "", "ssh timed out")
            return (0, '{"ok": true}', "")

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_capture)
        attach_mod._query_status("u@h", "")
        assert len(seen) == 2
        assert seen[0] == seen[1]
        assert "--revive" in seen[0]
        assert "|| magent up --json" in seen[0]
