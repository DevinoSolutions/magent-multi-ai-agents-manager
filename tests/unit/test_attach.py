import json
import subprocess
import sys
import types
from typing import ClassVar

import pytest

from magent import cli
from magent.config import SCHEMA_VERSION, MagentConfig, ProjectConfig, Settings
from magent.launch import eligible_psmux_projects
from tests.conftest import FakePlatform


def _cfg(projects, **settings):
    return MagentConfig(projects=projects, base_dir=None, settings=Settings(**settings))


# Every process name the corpse scan asks the OS about. Stated once here and
# pinned against the product constant by TestClientProcessNames, so the five
# call sites that assert "exactly one scan, of exactly these" don't each carry
# a literal that has to be edited when a new client kind is added.
_SCANNED_NAMES = ["ssh.exe", "psmux.exe", "magent-attach-client.exe"]

# A stand-in for the resolved magent-attach-client binary. Tests monkeypatch
# _attach_client_exe with this rather than letting shutil.which decide: the
# console script IS installed in the venv the suite runs from, so a test that
# trusted PATH would silently exercise a different pane command depending on
# how the developer invoked pytest.
_FAKE_SUPERVISOR = r"C:\venv\Scripts\magent-attach-client.EXE"


@pytest.fixture(autouse=True)
def _supervisor_on_path(monkeypatch):
    """Pin PATH resolution of the reconnect supervisor for the whole module.

    Without this the answer depends on how the suite was invoked (the console
    script exists inside the project venv, so `uv run pytest` resolves it and a
    bare `pytest` from another interpreter may not) -- and the resolution
    decides which command every attach pane is spawned with. Tests about the
    NOT-on-PATH fallback override this locally.
    """
    from magent.cli import attach as attach_mod

    monkeypatch.setattr(attach_mod, "_attach_client_exe", lambda: _FAKE_SUPERVISOR)


class _FakeProc:
    """Stand-in for a subprocess.Popen handle (the attach flow waits on the
    overlapped `serve --ensure` hop, so a bare None no longer suffices)."""

    def __init__(self, rc: int = 0) -> None:
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int:
        return self._rc

    def kill(self) -> None:  # pragma: no cover - only on the timeout path
        pass


def _fake_platform(monkeypatch, windows=None, **kwargs) -> FakePlatform:
    """Stand in for the platform the attach flow reaches for: the open-window
    snapshot it consults before spawning, the hotkey capability gate, and the
    post-tiling geometry nudge. Returns the double so a test can assert on it."""
    fp = FakePlatform(windows=dict(windows or {}), **kwargs)
    monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
    return fp


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

    def test_down_reason_renders_next_to_the_name(self, capsys):
        # A project whose folder no longer resolves was reported down forever
        # with zero explanation, here and in `up`. The reason is plain data on
        # the entry, so this renderer stays neutral between host and client.
        down = [
            {"name": "eBay", "group": "SALES", "reason": "folder not found"},
            {"name": "api", "group": "SALES"},
        ]
        cli._print_session_overview("host", [], down)
        out = capsys.readouterr().out
        assert "eBay (folder not found)" in out
        # A reason-less neighbour on the same line stays a bare name.
        assert "api (" not in out

    def test_no_reasons_leaves_the_names_bare(self, capsys):
        cli._print_session_overview("host", [], [{"name": "api", "group": "SALES"}])
        assert "api (" not in capsys.readouterr().out


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


class TestGeometryReclaim:
    """psmux 3.3.6 renders a session at whatever geometry the LAST client
    resize-or-attach event reported and never recomputes it, so a window this
    machine tiled back onto its own rect (the already-open case) keeps showing
    another client's size. After tiling, attach forces every window it handled
    to emit a client resize -- the same lever the manual Ctrl+/- zoom pulls."""

    def _reclaim(self, monkeypatch, titles, windows, **kwargs):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, windows, supports_nudge=True, **kwargs)
        attach_mod._reclaim_geometry(titles)
        return fp

    def test_every_resolved_window_is_nudged_in_one_batch(self, monkeypatch):
        fp = self._reclaim(
            monkeypatch,
            ["magent:api", "magent:web"],
            {"magent:api": 1, "magent:web": 2},
        )
        # One batch: the settle is shared, so a 40-window attach pays it once.
        assert fp.nudged == [[1, 2]]

    def test_badged_title_still_resolves(self, monkeypatch):
        # Same badge-proof matcher tiling resolved the window with moments ago.
        fp = self._reclaim(monkeypatch, ["magent:api"], {"magent:[!] api": 7})
        assert fp.nudged == [[7]]

    def test_window_that_vanished_is_skipped_not_fatal(self, monkeypatch):
        fp = self._reclaim(
            monkeypatch, ["magent:api", "magent:gone"], {"magent:api": 1}
        )
        assert fp.nudged == [[1]]

    def test_no_resolved_windows_never_calls_the_platform(self, monkeypatch):
        fp = self._reclaim(monkeypatch, ["magent:api"], {})
        assert fp.nudged == []

    def test_a_failing_nudge_does_not_raise(self, monkeypatch):
        # A window dying between the snapshot and the resize must not cost the
        # user their attach.
        fp = self._reclaim(
            monkeypatch,
            ["magent:api"],
            {"magent:api": 1},
            nudge_error=OSError("invalid window handle"),
        )
        assert fp.nudged == [[1]]

    def test_platform_without_the_capability_is_a_noop(self, monkeypatch):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, {"magent:api": 1})  # supports_nudge=False
        attach_mod._reclaim_geometry(["magent:api"])
        assert fp.nudged == []


class TestAttachFlowReclaimsGeometry:
    """The nudge runs on the real attach paths, after tiling (so the rect each
    window is restored to is the final tiled one) and for every title handled
    -- the deduped already-open ones very much included."""

    def _run(self, monkeypatch, sessions, windows, no_mux=False):
        from magent.cli import attach as attach_mod

        status = {
            "up": [{"name": s, "session": s} for s in sessions],
            "down": [],
            "projects": [{"name": s, "path": s} for s in sessions],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        monkeypatch.setattr(attach_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)

        fp = _fake_platform(monkeypatch, windows, supports_nudge=True)
        events: list[tuple[str, list]] = []
        monkeypatch.setattr(
            attach_mod, "_tile_titles", lambda t: events.append(("tile", list(t)))
        )
        real_nudge = fp.nudge_windows

        def spy(handles):
            events.append(("nudge", list(handles)))
            return real_nudge(handles)

        monkeypatch.setattr(fp, "nudge_windows", spy)

        attach_mod._attach_flow("user@host", no_mux=no_mux, group=None, yes=False)
        return events

    def test_psmux_path_nudges_after_tiling(self, monkeypatch):
        # `web` is freshly spawned (and so never lands in the snapshot here);
        # `api` is the already-open one -- exactly the stale-geometry case.
        events = self._run(monkeypatch, ["api", "web"], {"magent:api": 1})
        assert events == [("tile", ["magent:api", "magent:web"]), ("nudge", [1])]

    def test_deduped_windows_are_nudged_too(self, monkeypatch):
        events = self._run(
            monkeypatch, ["api", "web"], {"magent:api": 1, "magent:web": 2}
        )
        assert events[0][0] == "tile"
        assert events[1] == ("nudge", [1, 2])

    def test_nomux_path_nudges_after_tiling(self, monkeypatch):
        events = self._run(monkeypatch, ["api"], {"magent:api": 4}, no_mux=True)
        assert events == [("tile", ["magent:api"]), ("nudge", [4])]


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
        """Stub the heavy subsystem calls up_cmd makes; record revive args."""
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
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions", lambda names: names
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda names: names
        )
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
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda names: names
        )

        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        # The key is always present so a consumer can read it unconditionally.
        assert payload["revived"] == []

    def test_json_decorates_the_live_sessions(self, runner, tmp_path, monkeypatch):
        # `magent attach` drives the host through the --json path, so this is
        # the ONLY place a remotely-attached pre-existing session can pick up
        # the F1/F2 hints. Silent: the decoration talks to psmux, never stdout.
        seen: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"name": "api", "session": "api"}],
                [],
                _PROJECT_ROWS,
            ),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions_async", seen.append)

        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        assert seen == [["api"]]
        # stdout stays pure JSON despite the extra work.
        assert json.loads(result.stdout)["ok"] is True

    def test_json_never_uses_the_blocking_decoration(
        self, runner, tmp_path, monkeypatch
    ):
        # The regression this release fixes: the synchronous fan-out runs each
        # session's commands under a 3s-timeout subprocess.run, so a loaded host
        # spent ~15s per session decorating before printing a byte of JSON --
        # past the attach client's 30s status timeout, which retried with a 120s
        # one and re-ran the whole command. A status query must never wait on a
        # cosmetic status bar.
        def _blocking(*a, **k):
            raise AssertionError("`up --json` must not block on decoration")

        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"name": "api", "session": "api"}],
                [],
                _PROJECT_ROWS,
            ),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", _blocking)
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda names: names
        )

        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["ok"] is True

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


class TestUpDecorates:
    """Interactive `up` refreshes the F1/F2 status-line hints on every live
    session, so a session made before the feature (or by an older magent)
    gets them without being recreated."""

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

    def _patch(self, monkeypatch, up, down, created=()):
        seen: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (up, down, _PROJECT_ROWS),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux", lambda *a, **k: (list(created), [])
        )
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", seen.append)
        return seen

    def test_already_up_sessions_are_decorated(self, runner, tmp_path, monkeypatch):
        seen = self._patch(monkeypatch, up=[{"name": "api", "session": "api"}], down=[])
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert seen == [["api"]]

    def test_freshly_created_sessions_are_decorated_too(
        self, runner, tmp_path, monkeypatch
    ):
        seen = self._patch(
            monkeypatch,
            up=[],
            down=[{"name": "api", "session": "api"}],
            created=["api"],
        )
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert seen == [["api"]]


class TestUpReportsCasualties:
    """`up` reports what actually came up.

    `bring_up` used to discard the creation verify's answer and return every
    name it had attempted, so a wave whose sessions psmux never created still
    printed "Brought up N session(s)" -- while the very same run logged
    "session never came up after respawn; left down: ...".
    """

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

    def _patch(self, monkeypatch, *, created, failed):
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([], [{"name": "api", "session": "api"}], [{}]),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a, **k: [])
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux",
            lambda cfg, only=None, group=None: (list(created), list(failed)),
        )

    def test_failed_sessions_are_named(self, runner, tmp_path, monkeypatch):
        self._patch(monkeypatch, created=["web"], failed=["api"])
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert "Brought up 1" in result.output
        assert "1 session(s) failed to come up" in result.output
        assert "api" in result.output

    def test_a_clean_wave_says_nothing_about_failures(
        self, runner, tmp_path, monkeypatch
    ):
        self._patch(monkeypatch, created=["api"], failed=[])
        result = runner.invoke(cli.main, ["--config", self._config(tmp_path), "up"])
        assert result.exit_code == 0
        assert "failed to come up" not in result.output

    def test_json_is_a_pure_read_and_never_brings_anything_up(
        self, runner, tmp_path, monkeypatch
    ):
        # Why `up --json` carries no "failed" key: it never creates a session,
        # so there is no casualty set for it to report. `attach` polls this
        # command repeatedly; a bring-up here would type into panes on a poll.
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([], [{"name": "api", "session": "api"}], []),
        )
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda *a, **k: []
        )
        brought_up: list[object] = []
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux",
            lambda *a, **k: (brought_up.append(a), ([], []))[1],
        )
        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        assert brought_up == []
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["down"] == [{"name": "api", "session": "api"}]


class TestUpJsonVersion:
    """`up --json` advertises the host's magent version so the attach client
    can tell the user when the two machines are out of step."""

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

    def test_payload_carries_the_running_version(self, runner, tmp_path, monkeypatch):
        from magent import __version__

        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([], [], _PROJECT_ROWS),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions_async", lambda names: names
        )

        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_path), "up", "--json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["version"] == __version__


class TestAttachVersionSkew:
    """The attach CLIENT compares the host's `up --json` version against its
    own and warns -- non-fatally, on stderr -- when they differ or the host is
    too old to report one at all."""

    def _warn(self, capsys, status):
        from magent.cli import attach as attach_mod

        attach_mod._warn_version_skew("amin@desktop", status)
        return capsys.readouterr()

    def test_silent_when_versions_match(self, capsys):
        from magent import __version__

        captured = self._warn(capsys, {"version": __version__})
        assert captured.err == ""
        assert captured.out == ""

    def test_warns_when_the_host_reports_an_older_version(self, capsys):
        captured = self._warn(capsys, {"version": "3.1.4"})
        assert "amin@desktop runs magent 3.1.4" in captured.err
        assert "pip install -U magent-multi-ai-agents-manager" in captured.err
        # The warning must never contaminate stdout.
        assert captured.out == ""

    def test_warns_when_the_version_key_is_missing(self, capsys):
        # A host predating this release emits no `version` key at all -- the
        # exact case the warning exists for.
        captured = self._warn(capsys, {"ok": True, "up": [], "projects": []})
        assert "an older magent" in captured.err
        assert "pip install -U magent-multi-ai-agents-manager" in captured.err

    def test_a_mismatch_does_not_stop_the_attach(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        status = {
            "version": "3.1.4",
            "up": [{"name": "api", "session": "api"}],
            "down": [],
            "projects": [{"name": "api"}],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        monkeypatch.setattr(attach_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
        tiled: list[list[str]] = []
        monkeypatch.setattr(attach_mod, "_tile_titles", tiled.append)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        _fake_platform(monkeypatch)

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        captured = capsys.readouterr()
        assert "an older magent" not in captured.err
        assert "user@host runs magent 3.1.4" in captured.err
        # The flow still opened and tiled the window.
        assert tiled == [["magent:api"]]


class TestHotkeyCmdSshHost:
    """`magent hotkey --ssh-host` reaches the listener. The win32-only import
    is stubbed out the way the rest of the CLI suite stubs platform probes, so
    this runs on every OS in the matrix."""

    def _patch(self, monkeypatch):
        seen: list[tuple[str, str | None]] = []

        class _FakePlat:
            def supports_hotkey(self) -> bool:
                return True

        monkeypatch.setattr("magent.platform.get_platform", _FakePlat)

        fake = types.ModuleType("magent.hotkey")
        fake.listener_pid = lambda: None
        fake.run_hotkey = lambda url, ssh_host=None: seen.append((url, ssh_host))
        monkeypatch.setitem(sys.modules, "magent.hotkey", fake)
        return seen

    def test_ssh_host_is_forwarded_to_the_listener(self, runner, monkeypatch):
        seen = self._patch(monkeypatch)
        result = runner.invoke(
            cli.main, ["hotkey", "-s", "http://h:8033", "--ssh-host", "amin@deck"]
        )
        assert result.exit_code == 0
        assert seen == [("http://h:8033", "amin@deck")]

    def test_default_is_none_for_a_local_open(self, runner, monkeypatch):
        seen = self._patch(monkeypatch)
        result = runner.invoke(cli.main, ["hotkey", "-s", "http://h:8033"])
        assert result.exit_code == 0
        assert seen == [("http://h:8033", None)]


class TestMaybeStartHotkeySshHost:
    """The spawned listener's argv carries --ssh-host only when there is one."""

    def _args(self, monkeypatch, ssh_host):
        import magent.launch as launch_mod
        from magent.cli import background

        spawned: list[list[str]] = []

        class _FakePlat:
            def supports_hotkey(self) -> bool:
                return True

        monkeypatch.setattr("magent.platform.get_platform", _FakePlat)
        monkeypatch.setattr("magent.launch.spawn_detached", spawned.append)

        fake = types.ModuleType("magent.hotkey")
        fake.listener_pid = lambda: None
        # start_hotkey_listener imports the keep-or-restart pair alongside
        # listener_pid; with no listener running neither is called.
        fake.listener_manifest = lambda: None
        fake.stop_listener = lambda: False
        monkeypatch.setitem(sys.modules, "magent.hotkey", fake)
        # The spawn recipe itself moved to launch.start_hotkey_listener so the
        # launch path can share it; background is now just the capability gate.
        monkeypatch.setattr(launch_mod.time, "sleep", lambda s: None)

        background._maybe_start_hotkey("http://h:8033", ssh_host)
        return spawned[0]

    def test_ssh_host_is_passed_through(self, monkeypatch):
        args = self._args(monkeypatch, "amin@deck")
        assert args[-4:] == ["-s", "http://h:8033", "--ssh-host", "amin@deck"]

    def test_absent_ssh_host_adds_no_flag(self, monkeypatch):
        assert "--ssh-host" not in self._args(monkeypatch, None)


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


class TestCorpseDecision:
    """The pure half of dead-window repair.

    A window is a corpse when it is open but NO live local process is running
    its attach command. Keeping the decision pure means the risky half (closing
    someone's window) is pinned without a single real process, and the
    conservative bias -- when in doubt, DON'T close -- is testable directly.
    """

    def _corpses(self, open_sids, cmdlines):
        from magent.cli import attach as attach_mod

        return attach_mod._corpses(set(open_sids), list(cmdlines))

    def test_live_ssh_client_is_not_a_corpse(self):
        # What _attach_flow spawns: wt -- ssh -t user@host "psmux -L api attach || ..."
        cmd = "ssh -t user@host psmux -L api attach || magent sessions api"
        assert self._corpses(["api"], [cmd]) == set()

    def test_live_local_psmux_client_is_not_a_corpse(self):
        # What the launch path spawns (platform/windows.py::attach_psmux).
        assert self._corpses(["api"], ["psmux -L api attach"]) == set()

    def test_absolute_path_psmux_client_is_not_a_corpse(self):
        # attach_psmux execs the RESOLVED binary, so the real local cmdline
        # carries a full path. A marker anchored on the literal word "psmux "
        # missed it and scored every locally-launched window a corpse -- which
        # only became dangerous once _sweep_dead_windows started judging
        # windows outside the host's up list too.
        cmd = r"C:\Users\me\AppData\Local\psmux\psmux.exe -L api attach"
        assert self._corpses(["api"], [cmd]) == set()

    def test_quoted_session_id_still_counts_as_live(self):
        assert self._corpses(["api"], ['psmux -L "api" attach']) == set()
        assert self._corpses(["api"], ["psmux -L 'api' attach"]) == set()

    def test_no_client_at_all_is_a_corpse(self):
        # The reported bug: wt keeps the pane (and its magent: title) after
        # `client_loop: send disconnect` / `[process exited with code 255]`.
        assert self._corpses(["api"], ["ssh -t user@host echo hi"]) == {"api"}

    def test_empty_process_list_makes_every_open_window_a_corpse(self):
        assert self._corpses(["api", "web"], []) == {"api", "web"}

    def test_another_sessions_client_does_not_rescue_this_one(self):
        assert self._corpses(["api", "web"], ["psmux -L web attach"]) == {"api"}

    def test_a_longer_session_name_is_not_a_prefix_match(self):
        # `psmux -L api2 attach` must not read as a live client for "api":
        # the marker carries the trailing " attach", so the ids can't blur.
        assert self._corpses(["api"], ["psmux -L api2 attach"]) == {"api"}

    def test_matching_is_case_insensitive(self):
        assert self._corpses(["API"], ["PSMUX -L api ATTACH"]) == set()


class TestRepairCorpses:
    """The effectful half: scan, decide, close -- every step capability-gated,
    and a scan that could not run leaves every window alone."""

    def _repair(self, monkeypatch, open_sids, windows, **kwargs):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, windows, **kwargs)
        freed = attach_mod._repair_corpses(set(open_sids))
        return fp, freed

    def test_dead_window_is_closed_and_freed(self, monkeypatch):
        fp, freed = self._repair(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host echo hi"],
        )
        assert freed == {"api"}
        assert fp.closed == [7]
        # Only processes that can BE an attach client are worth scanning for.
        assert fp.scanned == [_SCANNED_NAMES]

    def test_live_window_is_left_alone(self, monkeypatch):
        fp, freed = self._repair(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L api attach || magent sessions api"],
        )
        assert freed == set()
        assert fp.closed == []

    def test_badged_title_resolves_to_the_same_window(self, monkeypatch):
        # titles.make_title("api", "needs-input") -> "magent:[!] api"
        fp, freed = self._repair(
            monkeypatch,
            ["api"],
            {"magent:[!] api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
        )
        assert freed == {"api"}
        assert fp.closed == [7]

    def test_failed_scan_closes_nothing(self, monkeypatch):
        # "We could not look" is not "nothing is running" -- acting on a failed
        # scan would close every LIVE window at once.
        fp, freed = self._repair(
            monkeypatch,
            ["api", "web"],
            {"magent:api": 7, "magent:web": 8},
            supports_close=True,
            supports_scan=True,
            scan_error=OSError("powershell exploded"),
        )
        assert freed == set()
        assert fp.closed == []

    def test_platform_without_the_capabilities_skips_detection(self, monkeypatch):
        # macOS/Linux clients take the ABC defaults: no scan, no close, and
        # today's title-only dedupe is preserved untouched.
        fp, freed = self._repair(monkeypatch, ["api"], {"magent:api": 7})
        assert freed == set()
        assert fp.closed == []
        assert fp.scanned == []

    def test_no_open_windows_never_scans(self, monkeypatch):
        fp, freed = self._repair(
            monkeypatch, [], {}, supports_close=True, supports_scan=True
        )
        assert freed == set()
        assert fp.scanned == []

    def test_a_handle_that_dies_mid_close_is_not_fatal(self, monkeypatch):
        _fp, freed = self._repair(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
            close_error=OSError("invalid window handle"),
        )
        assert freed == set()


class TestAttachReopensCorpseWindows:
    """End-to-end through _attach_flow: the corpse is closed AND respawned,
    which is the whole point -- title-only dedupe used to skip it forever."""

    def _run_flow(self, monkeypatch, sessions, windows, cmdlines):
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
            if args and args[0] == "wt":
                spawns.append(args)
            return _FakeProc()

        tiled: list[list[str]] = []
        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", tiled.append)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        fp = _fake_platform(
            monkeypatch,
            windows,
            supports_close=True,
            supports_scan=True,
            cmdlines=cmdlines,
        )
        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        return fp, spawns, tiled[0]

    def test_dead_window_is_closed_then_respawned(self, monkeypatch):
        fp, spawns, titles = self._run_flow(monkeypatch, ["api"], {"magent:api": 7}, [])
        assert fp.closed == [7]
        assert len(spawns) == 1
        assert spawns[0][-1] == "psmux -L api attach || magent sessions api"
        assert titles == ["magent:api"]

    def test_live_window_is_still_skipped(self, monkeypatch):
        fp, spawns, titles = self._run_flow(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            ["ssh -t user@host psmux -L api attach || magent sessions api"],
        )
        assert fp.closed == []
        assert spawns == []
        assert titles == ["magent:api"]

    def test_only_the_corpse_of_a_mixed_pair_is_reopened(self, monkeypatch):
        fp, spawns, titles = self._run_flow(
            monkeypatch,
            ["api", "web"],
            {"magent:api": 7, "magent:web": 8},
            ["ssh -t user@host psmux -L web attach || magent sessions web"],
        )
        assert fp.closed == [7]
        assert len(spawns) == 1
        assert spawns[0][-1] == "psmux -L api attach || magent sessions api"
        assert titles == ["magent:api", "magent:web"]


class TestStaleCorpseSweep:
    """The gap v3.10's corpse machinery left open.

    Repair only ever saw ``_already_open(up_sids)``: windows whose session was
    UP on the host at spawn time. A pane whose SESSION also died -- host
    rebooted, session killed earlier, or the user answered `n` / picked one
    group at the bring-up prompt -- was never scanned, never closed and never
    flagged, so a terminated terminal sat in the grid through every subsequent
    `magent attach`.

    The sweep now looks at every local magent: window. What protects another
    group's healthy windows is the LIVENESS check, never list membership --
    with `-g <group>` the host's up list is group-filtered, so perfectly good
    windows are legitimately "not up" from this run's point of view.
    """

    def _run_flow(self, monkeypatch, capsys, sessions, windows, cmdlines, **fp_kwargs):
        """Drive the psmux path with ``windows`` already on screen.

        ``sessions`` is what the host reports as up; ``windows`` is every
        magent: window this machine has, which is deliberately allowed to carry
        titles ``sessions`` knows nothing about. Spawned windows are NOT
        registered in the snapshot (mirroring TestAttachReopensCorpseWindows),
        so the post-tiling verification pass finds nothing to close and the
        assertions stay about the sweep.
        """
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
            if args and args[0] == "wt":
                spawns.append(args)
            return _FakeProc()

        tiled: list[list[str]] = []
        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda t: tiled.append(list(t)))
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        fp = _fake_platform(monkeypatch, windows, cmdlines=cmdlines, **fp_kwargs)
        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        return fp, spawns, tiled, capsys.readouterr().out

    _CAPABLE: ClassVar[dict[str, bool]] = {
        "supports_close": True,
        "supports_scan": True,
    }

    @staticmethod
    def _live(sid: str) -> str:
        return f"ssh -t user@host psmux -L {sid} attach || magent sessions {sid}"

    def test_dead_window_of_a_down_session_is_closed_and_not_respawned(
        self, monkeypatch, capsys
    ):
        # The reported bug: `ghost` died with a previous session and the host
        # does not list it at all, so nothing can be attached back to it.
        fp, spawns, tiled, out = self._run_flow(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            [self._live("api")],
            **self._CAPABLE,
        )
        assert fp.closed == [2]
        assert "magent:ghost" in out
        assert "session is not up" in out
        # Closed, never reopened: there is nothing on the host to attach to.
        assert spawns == []
        assert tiled == [["magent:api"]]

    def test_live_window_outside_the_up_list_is_left_strictly_alone(
        self, monkeypatch, capsys
    ):
        # `-g <group>` filters the host's up list, so another group's windows
        # look "not up" here while having a real client behind them.
        fp, spawns, tiled, out = self._run_flow(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:other": 2},
            [self._live("api"), self._live("other")],
            **self._CAPABLE,
        )
        assert fp.closed == []
        assert spawns == []
        assert "session is not up" not in out
        assert tiled == [["magent:api"]]

    def test_up_session_corpse_is_still_closed_and_reopened(self, monkeypatch, capsys):
        # The v3.10 behavior is untouched, and rides the same single scan as
        # the new stale half.
        fp, spawns, _tiled, out = self._run_flow(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            [],
            **self._CAPABLE,
        )
        assert fp.closed == [1, 2]
        assert "dead window closed -- reopening" in out
        assert "session is not up" in out
        # Only the up session comes back.
        assert len(spawns) == 1
        assert spawns[0][-1] == "psmux -L api attach || magent sessions api"

    def test_the_sweep_costs_exactly_one_process_scan(self, monkeypatch, capsys):
        fp, _spawns, _tiled, _out = self._run_flow(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:other": 2},
            [self._live("api"), self._live("other")],
            **self._CAPABLE,
        )
        # Two, and only two: the sweep's single scan (both halves partitioned
        # off ONE result) plus the pre-existing post-tiling verification pass.
        # Widening the sweep to every magent: window bought no extra scan.
        assert fp.scanned == [_SCANNED_NAMES] * 2

    def test_capability_less_platform_sweeps_nothing(self, monkeypatch, capsys):
        # macOS/Linux clients take the ABC defaults: no scan, no close, no
        # output -- and today's title-only dedupe is preserved untouched.
        fp, spawns, tiled, out = self._run_flow(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            [],
        )
        assert fp.closed == []
        assert fp.scanned == []
        assert "dead window closed" not in out
        assert spawns == []
        assert tiled == [["magent:api"]]


class TestSweepDeadWindows:
    """The sweep helper on its own: what it closes, and what it hands back to
    the spawn loop as still-open."""

    def _sweep(self, monkeypatch, up_sids, windows, **kwargs):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, windows, **kwargs)
        return fp, attach_mod._sweep_dead_windows(up_sids)

    def test_returns_the_live_up_windows_only(self, monkeypatch):
        fp, open_already = self._sweep(
            monkeypatch,
            ["api", "web"],
            {"magent:api": 1, "magent:web": 2, "magent:ghost": 3},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L web attach || magent sessions web"],
        )
        # api's corpse is closed (and dropped, so the spawn loop reopens it);
        # ghost is closed and simply gone; web survives as already-open.
        assert open_already == {"web"}
        assert fp.closed == [1, 3]
        # Both halves come off ONE process scan.
        assert fp.scanned == [_SCANNED_NAMES]

    def test_a_failed_scan_closes_nothing_and_keeps_every_up_window(self, monkeypatch):
        fp, open_already = self._sweep(
            monkeypatch,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            supports_close=True,
            supports_scan=True,
            scan_error=OSError("powershell exploded"),
        )
        assert open_already == {"api"}
        assert fp.closed == []

    def test_no_windows_at_all_never_scans(self, monkeypatch):
        fp, open_already = self._sweep(
            monkeypatch, ["api"], {}, supports_close=True, supports_scan=True
        )
        assert open_already == set()
        assert fp.scanned == []

    def test_a_bare_prefix_title_is_never_a_close_target(self, monkeypatch):
        # A degenerate `magent:` window parses to an empty name; it belongs to
        # no session and must not become collateral.
        fp, open_already = self._sweep(
            monkeypatch,
            ["api"],
            {"magent:": 1},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
        )
        assert open_already == set()
        assert fp.closed == []
        assert fp.scanned == []


class TestSpawnRetryAfterHandshakeFailure:
    """A big attach opens one SSH connection per window; during a bring-up
    storm the host's sshd hits MaxStartups and drops some of them, leaving
    `[process exited with code 255]` panes. Corpse repair alone only healed
    those on the NEXT attach -- so after tiling (by when a handshake has either
    connected or died) the flow scans the windows it just opened and respawns
    the casualties, once, more slowly."""

    def _run(self, monkeypatch, sessions, live_per_scan, capsys=None, **fp_kwargs):
        """Drive the psmux path with a scripted process scan.

        ``live_per_scan[i]`` is the set of session ids whose attach client is
        running at the i-th scan (the last entry repeats), so a test can say
        "dead at the verification scan, alive after the respawn" without a
        single real process. Spawned windows register in the platform snapshot,
        which is what makes a corpse findable (and closeable) at all.
        """
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
        fp = _fake_platform(monkeypatch, {}, **fp_kwargs)

        spawns: list[list[str]] = []

        def fake_popen(args, **k):
            if args and args[0] == "wt":
                spawns.append(args)
                # A real `wt` window appears (and keeps its title) whether or
                # not its ssh survives -- that is the whole corpse problem.
                fp._register_window(args[args.index("--title") + 1])
            return _FakeProc()

        scans: list[list[str]] = []

        def fake_cmdlines(names):
            scans.append(list(names))
            live = live_per_scan[min(len(scans) - 1, len(live_per_scan) - 1)]
            return [
                f"ssh -t user@host psmux -L {s} attach || magent sessions {s}"
                for s in live
            ]

        monkeypatch.setattr(fp, "process_cmdlines", fake_cmdlines)
        sleeps: list[float] = []
        tiled: list[list[str]] = []
        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda t: tiled.append(list(t)))
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", sleeps.append)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        out = capsys.readouterr().out if capsys is not None else ""
        return fp, spawns, scans, sleeps, tiled, out

    # The capabilities a Windows attach client has; a platform lacking them
    # skips the whole verification pass (see the last test).
    _CAPABLE: ClassVar[dict[str, bool]] = {
        "supports_close": True,
        "supports_scan": True,
    }

    def test_casualty_is_respawned_with_the_retry_stagger(self, monkeypatch, capsys):
        from magent.cli import attach as attach_mod

        fp, spawns, _scans, sleeps, tiled, out = self._run(
            monkeypatch,
            ["api", "web"],
            [["web"], ["api", "web"]],  # api dies at the handshake, then sticks
            capsys=capsys,
            **self._CAPABLE,
        )
        # The corpse pane is closed, and only that session is opened again.
        assert len(spawns) == 3
        assert spawns[-1][-1] == "psmux -L api attach || magent sessions api"
        # The retry batch goes through the same _spawn_windows, so a respawned
        # window is supervised (and therefore reconnecting) exactly like the
        # first batch -- it must not silently regress to a bare ssh that can
        # only die once.
        for argv in spawns:
            assert argv[argv.index("--") + 1] == _FAKE_SUPERVISOR
            assert (
                attach_mod._corpses(
                    {argv[argv.index("--session") + 1]}, [subprocess.list2cmdline(argv)]
                )
                == set()
            )
        assert fp.closed == [1]  # handle of the first `magent:api` window
        # First pass keeps the fast stagger; the retry slows down deliberately,
        # then a short bounded settle precedes the single re-check.
        assert sleeps == [
            attach_mod._SPAWN_STAGGER_S,
            attach_mod._SPAWN_STAGGER_S,
            attach_mod._RETRY_STAGGER_S,
            attach_mod._RETRY_SETTLE_S,
        ]
        # Only the respawned subset is re-tiled.
        assert tiled == [["magent:api", "magent:web"], ["magent:api"]]
        assert "died during SSH handshake" in out
        # It came back: no "re-run attach" nag.
        assert "Re-run" not in out

    def test_survivor_of_two_spawns_warns_and_stops(self, monkeypatch, capsys):
        fp, spawns, scans, _sleeps, _tiled, out = self._run(
            monkeypatch,
            ["api"],
            [[]],  # never comes up, on either attempt
            capsys=capsys,
            **self._CAPABLE,
        )
        # Two spawns is the budget: one original + exactly one retry.
        assert len(spawns) == 2
        # Two scans: the verification one and the single read-only re-check.
        assert len(scans) == 2
        # The re-check closes nothing -- the pane carries the ssh error text.
        assert fp.closed == [1]
        assert "api" in out
        assert "still could not connect to user@host" in out
        assert "magent attach" in out

    def test_zero_casualties_costs_one_scan_and_no_output(self, monkeypatch, capsys):
        fp, spawns, scans, _sleeps, tiled, out = self._run(
            monkeypatch,
            ["api", "web"],
            [["api", "web"]],
            capsys=capsys,
            **self._CAPABLE,
        )
        assert len(spawns) == 2  # nothing respawned
        assert scans == [_SCANNED_NAMES]  # exactly one scan
        assert fp.closed == []
        assert tiled == [["magent:api", "magent:web"]]
        assert "died during SSH handshake" not in out
        assert "still could not connect" not in out

    def test_platform_without_the_capabilities_skips_verification(
        self, monkeypatch, capsys
    ):
        # macOS/Linux clients take the ABC defaults: no scan, no respawn.
        fp, spawns, scans, _sleeps, tiled, out = self._run(
            monkeypatch, ["api", "web"], [[]], capsys=capsys
        )
        assert scans == []
        assert len(spawns) == 2
        assert fp.closed == []
        assert tiled == [["magent:api", "magent:web"]]
        assert "died during SSH handshake" not in out


class TestDeadWindowAnnotation:
    """`_print_session_overview` is pure HOST truth -- a psmux session that
    exists renders green `N/N ready` even when the pane on this machine is a
    corpse. The attach call site annotates that, without teaching the shared
    renderer (used by host-side `up` too) about local windows."""

    def _annotate(self, monkeypatch, capsys, sessions, windows, **kwargs):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, windows, **kwargs)
        attach_mod._annotate_dead_windows([{"name": s, "session": s} for s in sessions])
        return fp, capsys.readouterr().out

    def test_dead_windows_are_named_as_reopenable(self, monkeypatch, capsys):
        fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api", "web"],
            {"magent:api": 1, "magent:web": 2},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L web attach || magent sessions web"],
        )
        assert "1 window(s) here are dead" in out
        assert "closed and reopened" in out
        assert "api" in out
        # Read-only: the close belongs to the spawn phase's own fresh scan.
        assert fp.closed == []

    def test_dead_window_of_a_down_session_gets_its_own_wording(
        self, monkeypatch, capsys
    ):
        # `ghost` is not in the up list at all, so "will be closed and
        # reopened" would be a lie -- there is nothing to reopen it against.
        fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L api attach || magent sessions api"],
        )
        assert "1 dead window(s) from a previous session (session down)" in out
        assert "will be closed:" in out
        assert "ghost" in out
        assert "closed and reopened" not in out
        assert fp.closed == []

    def test_both_kinds_render_as_two_lines_off_one_scan(self, monkeypatch, capsys):
        fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:ghost": 2},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
        )
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert "window(s) here are dead" in lines[0]
        assert "closed and reopened" in lines[0]
        assert "from a previous session (session down)" in lines[1]
        # One process scan feeds both halves; the partition is on the RESULT.
        assert fp.scanned == [_SCANNED_NAMES]
        assert fp.closed == []

    def test_a_live_window_outside_the_up_list_is_not_annotated(
        self, monkeypatch, capsys
    ):
        _fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1, "magent:other": 2},
            supports_close=True,
            supports_scan=True,
            cmdlines=[
                "ssh -t user@host psmux -L api attach || magent sessions api",
                "ssh -t user@host psmux -L other attach || magent sessions other",
            ],
        )
        assert out == ""

    def test_all_live_prints_nothing(self, monkeypatch, capsys):
        _fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L api attach || magent sessions api"],
        )
        assert out == ""

    def test_session_without_a_local_window_is_not_dead(self, monkeypatch, capsys):
        # Nothing open here yet is the normal first-attach case, not a corpse.
        _fp, out = self._annotate(
            monkeypatch, capsys, ["api"], {}, supports_close=True, supports_scan=True
        )
        assert out == ""

    def test_failed_scan_prints_nothing(self, monkeypatch, capsys):
        _fp, out = self._annotate(
            monkeypatch,
            capsys,
            ["api"],
            {"magent:api": 1},
            supports_close=True,
            supports_scan=True,
            scan_error=OSError("powershell exploded"),
        )
        assert out == ""

    def test_capability_less_platform_never_scans(self, monkeypatch, capsys):
        fp, out = self._annotate(monkeypatch, capsys, ["api"], {"magent:api": 1})
        assert out == ""
        assert fp.scanned == []

    def test_annotation_lands_before_the_bring_up_prompt(self, monkeypatch, capsys):
        """Ordering is the point: the user must see it while choosing."""
        import click

        from magent.cli import attach as attach_mod

        status = {
            "up": [{"name": "api", "session": "api"}],
            "down": [{"name": "web", "session": "web", "group": "g"}],
            "projects": [{"name": "api"}, {"name": "web"}],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        monkeypatch.setattr(attach_mod.subprocess, "Popen", lambda *a, **k: _FakeProc())
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda t: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        _fake_platform(
            monkeypatch,
            {"magent:api": 1},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
        )
        # "none": leave the down session alone, so the flow stops at the prompt
        # decision and the ordering assertion is about the overview alone.
        monkeypatch.setattr(click, "prompt", lambda *a, **k: "n")

        attach_mod._attach_flow("user@host", no_mux=False, group=None, yes=False)
        out = capsys.readouterr().out
        assert out.index("window(s) here are dead") < out.index("Bring up")


class TestSpawnWindows:
    """The extracted per-sid spawn helper: same wt line both passes use, same
    already-open dedupe, and the stagger is the caller's to choose."""

    def _spawn(self, monkeypatch, sids, open_already, stagger, **kwargs):
        from magent.cli import attach as attach_mod

        spawns: list[list[str]] = []
        sleeps: list[float] = []
        monkeypatch.setattr(
            attach_mod.subprocess, "Popen", lambda args, **k: spawns.append(args)
        )
        monkeypatch.setattr(attach_mod.time, "sleep", sleeps.append)
        titles = attach_mod._spawn_windows(
            "user@host", sids, set(open_already), stagger, **kwargs
        )
        return spawns, sleeps, titles

    def test_returns_titles_in_order_and_pays_the_given_stagger(self, monkeypatch):
        spawns, sleeps, titles = self._spawn(monkeypatch, ["api", "web"], [], 1.0)
        assert titles == ["magent:api", "magent:web"]
        assert sleeps == [1.0, 1.0]
        assert spawns[0][-1] == "psmux -L api attach || magent sessions api"

    def test_already_open_is_titled_but_neither_spawned_nor_staggered(
        self, monkeypatch
    ):
        spawns, sleeps, titles = self._spawn(monkeypatch, ["api", "web"], ["api"], 0.25)
        assert titles == ["magent:api", "magent:web"]
        assert len(spawns) == 1
        assert sleeps == [0.25]

    def test_the_pane_title_is_locked_against_the_program_inside_it(self, monkeypatch):
        # A supervised pane is the loudest writer magent has: on every redial
        # the supervisor prints status lines, ssh prints its own, and the remote
        # agent emits OSC title escapes for the whole session. The wt title lock
        # is what keeps `magent:api` on the window through all of it -- and the
        # corpse scanner depends on exactly that (it pairs windows to processes
        # BECAUSE the title outlives the process that named it).
        for reconnect in (True, False):
            spawns, _sleeps, _titles = self._spawn(
                monkeypatch, ["api"], [], 0.0, reconnect=reconnect
            )
            argv = spawns[0]
            assert "--suppressApplicationTitle" in argv
            # ...and before the `--`, or wt would pass it to the pane command.
            assert argv.index("--suppressApplicationTitle") < argv.index("--")
            assert argv[argv.index("--title") + 1] == "magent:api"

    def test_pane_runs_the_reconnect_supervisor_by_default(self, monkeypatch):
        # The headline change: a pane is no longer a bare ssh that dies with
        # its connection. wt drives the supervisor, the supervisor drives ssh.
        spawns, _sleeps, _titles = self._spawn(monkeypatch, ["api"], [], 0.0)
        argv = spawns[0]
        assert argv[argv.index("--") + 1] == _FAKE_SUPERVISOR
        assert "ssh" not in argv
        assert argv[-4:] == [
            "--session",
            "api",
            "--remote",
            "psmux -L api attach || magent sessions api",
        ]
        assert argv[argv.index("--target") + 1] == "user@host"

    def test_supervisor_argv_carries_the_attach_marker(self, monkeypatch):
        # COHERENCE PIN. During a backoff sleep the supervisor is the only
        # process left, so if its command line stopped carrying the session's
        # attach marker, _dead_sids would call a reconnecting pane a corpse and
        # _sweep_dead_windows would close the window that was healing itself.
        from magent.cli import attach as attach_mod

        spawns, _sleeps, _titles = self._spawn(monkeypatch, ["api"], [], 0.0)
        # Exactly the shape a Windows process table reports for this spawn.
        cmdline = subprocess.list2cmdline(spawns[0])
        assert attach_mod._corpses({"api"}, [cmdline]) == set()

    def test_no_reconnect_spawns_the_historical_bare_ssh_pane(self, monkeypatch):
        # Without keepalives, a post-sleep ssh whose TCP connection died hangs
        # forever and is indistinguishable from a live client to _dead_sids --
        # the pane is frozen while the overview reports it ready. They must
        # also land as ssh OPTIONS (before -t, i.e. before the target and the
        # remote command), or ssh would read them as part of the command line.
        spawns, _sleeps, _titles = self._spawn(
            monkeypatch, ["api"], [], 0.0, reconnect=False
        )
        argv = spawns[0]
        i_ssh, i_t = argv.index("ssh"), argv.index("-t")
        assert argv[i_ssh + 1 : i_t] == [
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "ConnectTimeout=20",
        ]
        assert i_t < argv.index("user@host")
        # ...and the attach marker _dead_sids scans for is untouched.
        assert argv[-1] == "psmux -L api attach || magent sessions api"

    def test_supervisor_off_path_degrades_to_bare_ssh_and_says_so(
        self, monkeypatch, capsys
    ):
        # A stale editable install, or a PATH exposing `magent` without its
        # siblings. Forty windows that fail to start would be far worse than
        # forty windows with the old behavior plus one honest warning.
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(attach_mod, "_attach_client_exe", lambda: None)
        spawns, _sleeps, _titles = self._spawn(monkeypatch, ["api", "web"], [], 0.0)
        out = capsys.readouterr().out
        assert "magent-attach-client" in out
        assert "will not auto-reconnect" in out
        # Warned once for the batch, not once per window.
        assert out.count("not on PATH") == 1
        for argv in spawns:
            assert argv[argv.index("--") + 1] == "ssh"

    def test_no_reconnect_never_warns_about_a_binary_it_does_not_want(
        self, monkeypatch, capsys
    ):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(attach_mod, "_attach_client_exe", lambda: None)
        self._spawn(monkeypatch, ["api"], [], 0.0, reconnect=False)
        assert "not on PATH" not in capsys.readouterr().out


class TestClientProcessNames:
    """The scan's name list, pinned once so the five 'exactly one scan of
    exactly these' assertions elsewhere can reference it."""

    def test_the_scan_covers_every_kind_of_attach_client(self):
        from magent.cli import attach as attach_mod

        assert attach_mod._CLIENT_PROCESS_NAMES == _SCANNED_NAMES

    def test_every_name_survives_the_platform_scan_filter(self):
        # platform/windows.py::process_cmdlines drops any name that is not
        # alnum-after-stripping-dots-and-dashes, to keep the CIM filter free of
        # an injection seam. A name that silently fails that check would remove
        # a whole client kind from the scan without a single test going red.
        for name in _SCANNED_NAMES:
            assert name.replace(".", "").replace("-", "").isalnum(), name


class TestDeadSids:
    """The read-only corpse check the post-retry verification uses: it must
    decide exactly like _repair_corpses and close nothing."""

    def _dead(self, monkeypatch, open_sids, windows, **kwargs):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, windows, **kwargs)
        return fp, attach_mod._dead_sids(set(open_sids))

    def test_dead_session_is_reported_without_closing(self, monkeypatch):
        fp, dead = self._dead(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=[],
        )
        assert dead == {"api"}
        assert fp.closed == []

    def test_live_session_is_not_reported(self, monkeypatch):
        _fp, dead = self._dead(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            cmdlines=["ssh -t user@host psmux -L api attach || magent sessions api"],
        )
        assert dead == set()

    def test_failed_scan_reports_nothing(self, monkeypatch):
        _fp, dead = self._dead(
            monkeypatch,
            ["api"],
            {"magent:api": 7},
            supports_close=True,
            supports_scan=True,
            scan_error=OSError("powershell exploded"),
        )
        assert dead == set()

    def test_capability_less_platform_reports_nothing(self, monkeypatch):
        fp, dead = self._dead(monkeypatch, ["api"], {"magent:api": 7})
        assert dead == set()
        assert fp.scanned == []

    def test_empty_input_never_scans(self, monkeypatch):
        fp, dead = self._dead(
            monkeypatch, [], {}, supports_close=True, supports_scan=True
        )
        assert dead == set()
        assert fp.scanned == []


class TestCloseAttachWindows:
    """`down --host` closes the local windows before the remote kill strands
    them. An empty selection means every magent: window."""

    def test_named_subset_closes_only_those_windows(self, monkeypatch):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(
            monkeypatch,
            {"magent:api": 1, "magent:web": 2, "magent:db": 3},
            supports_close=True,
        )
        assert attach_mod._close_attach_windows(["api", "db"]) == 2
        assert fp.closed == [1, 3]

    def test_empty_selection_closes_every_attach_window(self, monkeypatch):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(
            monkeypatch,
            {"magent:api": 1, "magent:[!] web": 2, "Some Other App": 9},
            supports_close=True,
        )
        assert attach_mod._close_attach_windows(()) == 2
        # The non-magent window is never touched; the badged one still matches.
        assert sorted(fp.closed) == [1, 2]

    def test_platform_without_close_support_is_a_no_op(self, monkeypatch):
        from magent.cli import attach as attach_mod

        fp = _fake_platform(monkeypatch, {"magent:api": 1})
        assert attach_mod._close_attach_windows(["api"]) == 0
        assert fp.closed == []


class TestRemoteDownCommand:
    """The host-side line must carry the user's selection VERBATIM -- a `down`
    that quietly widened or narrowed its scope over SSH would be worse than the
    no-op it replaces."""

    def _cmd(self, names=(), group=None, do_all=False, stop_srv=False):
        from magent.cli import attach as attach_mod

        return attach_mod._remote_down_command(names, group, do_all, stop_srv)

    def test_bare(self):
        assert self._cmd() == "magent down"

    def test_all(self):
        assert self._cmd(do_all=True) == "magent down --all"

    def test_server(self):
        assert self._cmd(stop_srv=True) == "magent down --server"

    def test_group_is_quoted_like_every_other_remote_command(self):
        assert self._cmd(group="core one") == 'magent down -g "core one"'

    def test_names_are_forwarded_in_order(self):
        assert self._cmd(names=("api", "web")) == 'magent down "api" "web"'

    def test_everything_at_once(self):
        assert (
            self._cmd(names=("api",), group="core", do_all=True, stop_srv=True)
            == 'magent down "api" -g "core" --all --server'
        )


class TestRemoteDown:
    """SSH failure is a loud, non-zero result -- never a silent no-op."""

    def _run(self, monkeypatch, rc, out="", err="", names=()):
        from magent.cli import attach as attach_mod

        order: list[str] = []

        def fake_close(sids):
            order.append(f"close:{list(sids)}")
            return 1

        def fake_ssh(target, remote_cmd, timeout=30, stdin_text=None):
            order.append(f"ssh:{remote_cmd}")
            return rc, out, err

        monkeypatch.setattr(attach_mod, "_close_attach_windows", fake_close)
        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_ssh)
        code = attach_mod._remote_down("u@h", names, None, True, False)
        return code, order

    def test_success_returns_zero(self, monkeypatch):
        code, _ = self._run(monkeypatch, 0, out="  + Stopped 3 session(s)")
        assert code == 0

    def test_windows_are_closed_before_the_remote_kill(self, monkeypatch):
        # Otherwise the kill strands one corpse window per session -- bug 1,
        # recreated at scale.
        _code, order = self._run(monkeypatch, 0, names=("api",))
        assert order == ["close:['api']", 'ssh:magent down "api" --all']

    def test_ssh_failure_is_surfaced_and_propagated(self, monkeypatch):
        code, _ = self._run(monkeypatch, 255, err="ssh: connect to host: timed out")
        assert code == 255

    def test_timeout_is_propagated(self, monkeypatch):
        code, _ = self._run(monkeypatch, 124, err="ssh timed out")
        assert code == 124
