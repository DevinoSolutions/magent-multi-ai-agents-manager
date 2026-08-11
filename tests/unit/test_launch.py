"""Unit tests for magent.launch.run_magent's no-monitors error path
(F-D2-003 / F-D2-001: there was previously no test_launch.py at all).

Cross-platform: FakePlatform (tests/conftest.py) stands in for a real
Platform, so this exercises launch.py's `-> int` return-code contract without
touching any OS-specific window/monitor API.
"""

from __future__ import annotations

import os
import time

import pytest

from magent.config import MagentConfig, ProjectConfig, Settings, WindowConfig
from magent.grid import MonitorRect, Rect, compute_grid
from magent.launch import (
    RunOpts,
    _dispatch_cli_agent_project,
    _dispatch_ide_project,
    _expand_base_dir,
    _launch_projects,
    _LaunchResult,
    _prepare_grid,
    _select_projects,
    _start_psmux_and_upload,
    _Target,
    _tile_targets,
    eligible_psmux_projects,
    hotkey_restart_reason,
    run_magent,
)
from magent.platform import PsmuxWindowOpts, TerminalNotFoundError
from tests.conftest import FakePlatform


@pytest.fixture(autouse=True)
def _no_real_psmux_probe(monkeypatch):
    """`_start_psmux_and_upload` now runs the same creation verify the attach
    path's `bring_up` does, and that verify shells out to the host's psmux
    binary. A unit test must not depend on whether the machine running it has
    psmux installed, so the binary reads as absent (the verify's documented
    no-op) unless a test opts back in -- TestGoPathCreationVerify does."""
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)


class TestNoMonitors:
    def test_returns_2_and_logs_error(self, monkeypatch, caplog):
        # FakePlatform's list_monitors() needs only monitors=[] and a no-op
        # set_dpi_aware() -- the no-monitors guard returns before
        # snapshot_windows or anything else on Platform is touched.
        fp = FakePlatform(monitors=[])
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(projects=[])

        with caplog.at_level("ERROR", logger="magent.launch"):
            rc = run_magent(cfg, RunOpts())

        assert rc == 2
        assert "no monitors detected" in caplog.text
        assert fp.dpi_aware_calls == 1  # set_dpi_aware still runs before the check


@pytest.fixture
def fake_sleep(monkeypatch):
    """Patches the real time.sleep function object -- launch.py's per-window
    launch_delay_ms sleep and tiling.py's retry-loop sleeps both do a
    module-level `import time`, so they share sys.modules['time'] and this
    one patch intercepts both (same convention as tests/unit/test_tiling.py's
    fake_sleep). Records each sleep's duration."""
    calls: list[float] = []

    def _sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(time, "sleep", _sleep)
    return calls


class TestRunMagentCharacterization:
    """Whole-function behavior pins for run_magent (R4), written BEFORE
    the phase extraction so every later extraction step is judged against
    locked behavior. Each test drives run_magent directly (not through
    the CLI) and asserts on the fake_platform double's call record -- never
    on full-output equality (style/spacing may drift)."""

    def test_happy_local_cli_agent_launches_then_tiles(
        self, fake_platform, tmp_path, fake_sleep
    ):
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        assert len(fake_platform.launched_terminals) == 1
        assert fake_platform.launched_terminals[0].title == "magent:proj"
        assert len(fake_platform.moved) == 1
        assert fake_platform.moved[0][1] == Rect(x=0, y=0, w=960, h=1080)

    def test_dry_run_launches_and_moves_nothing(
        self, fake_platform, tmp_path, fake_sleep, capsys
    ):
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts(dry_run=True))

        assert rc == 0
        assert fake_platform.launched_terminals == []
        assert fake_platform.launched_vscode == []
        assert fake_platform.moved == []
        assert "DRY RUN" in capsys.readouterr().out

    def test_ide_project_launches_vscode(self, fake_platform, tmp_path, fake_sleep):
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="code")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        assert len(fake_platform.launched_vscode) == 1
        assert fake_platform.launched_vscode[0].command == "code"

    def test_psmux_path_collects_and_attaches(self, monkeypatch, tmp_path, fake_sleep):
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        assert len(fp.launched_psmux) == 1
        assert len(fp.attached_psmux) == 1
        assert fp.launched_terminals == []

    def test_empty_group_returns_zero(
        self, fake_platform, tmp_path, fake_sleep, capsys
    ):
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts(group="nope"))

        assert rc == 0
        assert fake_platform.launched_terminals == []
        assert "No projects in group" in capsys.readouterr().err

    def test_retile_all_places_running_window(self, monkeypatch, tmp_path, fake_sleep):
        # Live windows carry magent:-grammar titles (possibly badged); resolution
        # goes through parse_title, so a badge must not break retiling.
        fp = FakePlatform(windows={"magent:[!] proj": 555})
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts(retile_all=True))

        assert rc == 0
        assert fp.launched_terminals == []
        assert (555, Rect(x=0, y=0, w=960, h=1080)) in fp.moved

    def test_terminal_not_found_aborts_with_hint(
        self, monkeypatch, tmp_path, fake_sleep, capsys
    ):
        # TF-W-001: when launch_terminal raises TerminalNotFoundError (e.g. wt
        # not installed), run_magent aborts cleanly -- rc 2, the actionable
        # install hint on stderr, and no tiling -- never a raw traceback.
        fp = FakePlatform()

        def _boom(_opts):
            raise TerminalNotFoundError(
                "Windows Terminal (wt) not found -- install: "
                "winget install Microsoft.WindowsTerminal, then re-run."
            )

        monkeypatch.setattr(fp, "launch_terminal", _boom)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 2
        err = capsys.readouterr().err
        assert "winget install Microsoft.WindowsTerminal" in err
        assert fp.moved == []  # aborted before the tiling phase


class TestTileTargets:
    """Direct unit test for the extracted tile phase (R4, Step 2)."""

    def test_moves_present_and_reports_missing(self, fake_sleep, capsys):
        fp = FakePlatform(windows={"present": 42})
        slots = compute_grid(
            [MonitorRect(x=0, y=0, w=1920, h=1080, is_primary=True, scale_factor=1.0)],
            2,
            1,
        )
        targets = [
            _Target(name="present", key="present", mode="exact", is_new=True),
            _Target(name="absent", key="absent", mode="exact", is_new=True),
        ]

        _tile_targets(fp, RunOpts(), slots, targets)

        assert len(fp.moved) == 1
        assert fp.moved[0] == (42, Rect(x=0, y=0, w=960, h=1080))
        assert "not found" in capsys.readouterr().out


class TestStartPsmuxAndUpload:
    """Direct unit tests for the extracted psmux+upload-server phase (R4,
    Step 3; renamed from the plan's _bring_up_psmux -- launch.py already has
    a public bring_up_psmux for the attach-path session creator). Takes the
    explicit-args form narrowed to _LaunchResult in Step 4."""

    def test_attaches_each_window(self):
        fp = FakePlatform(supports_psmux=True)
        windows = [
            PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude"),
            PsmuxWindowOpts(window_name="b", cwd="/tmp/b", command="claude"),
        ]
        colors = {"a": "#111111", "b": None}
        cfg = MagentConfig(projects=[])
        result = _LaunchResult(targets=[], psmux_windows=windows, psmux_colors=colors)

        _start_psmux_and_upload(fp, cfg, RunOpts(), result)

        assert fp.launched_psmux == windows
        assert len(fp.attached_psmux) == 2
        assert fp.attached_psmux[0] == ("a", "magent:a", "#111111", None)
        assert fp.attached_psmux[1] == ("b", "magent:b", None, None)

    def test_noop_on_dry_run(self):
        fp = FakePlatform(supports_psmux=True)
        windows = [PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude")]
        cfg = MagentConfig(projects=[])
        result = _LaunchResult(
            targets=[], psmux_windows=windows, psmux_colors={"a": None}
        )

        _start_psmux_and_upload(fp, cfg, RunOpts(dry_run=True), result)

        assert fp.launched_psmux == []
        assert fp.attached_psmux == []


class TestGoPathCreationVerify:
    """The --go path reaches psmux through the same `launch_psmux_session` the
    attach path's `bring_up` does, so it shares the creation verify: a session
    that never came up is named in the log and respawned once. Without this,
    only `magent attach`/`up` would have proof a session exists."""

    def _run(self, monkeypatch, *, missing):
        fp = FakePlatform(supports_psmux=True, psmux_launch_failures=set(missing))
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.psmux.time.sleep", lambda _s: None)
        monkeypatch.setattr(
            "magent.psmux.has_session",
            lambda name, psmux=None, timeout=None: name in fp.psmux_sessions,
        )
        windows = [
            PsmuxWindowOpts(window_name=n, cwd=f"/tmp/{n}", command="claude")
            for n in ("a", "b")
        ]
        result = _LaunchResult(
            targets=[], psmux_windows=windows, psmux_colors={"a": None, "b": None}
        )
        _start_psmux_and_upload(fp, MagentConfig(projects=[]), RunOpts(), result)
        return fp

    def test_a_session_that_never_came_up_is_respawned(self, monkeypatch):
        fp = self._run(monkeypatch, missing=["a"])
        assert fp.psmux_launches == [["a", "b"], ["a"]]

    def test_a_healthy_go_launch_is_never_respawned(self, monkeypatch):
        fp = self._run(monkeypatch, missing=[])
        assert fp.psmux_launches == [["a", "b"]]

    def test_attach_still_covers_every_window(self, monkeypatch):
        # The verify is additive: the respawn must not change which windows
        # get attached, nor attach the recreated one twice.
        fp = self._run(monkeypatch, missing=["a"])
        assert [c[0] for c in fp.attached_psmux] == ["a", "b"]


class TestHotkeyRestartReason:
    """The keep-or-restart decision for an already-running Alt+V/F2 listener.

    Deliberately OS-agnostic: `magent.hotkey` raises ImportError off win32, so
    the decision lives in launch.py where every runner can exercise it.
    """

    CURRENT = "9.9.9-test"

    @pytest.fixture(autouse=True)
    def _pin_version(self, monkeypatch):
        monkeypatch.setattr("magent.__version__", self.CURRENT, raising=False)

    def _manifest(self, **over):
        base = {
            "version": self.CURRENT,
            "server_url": "http://127.0.0.1:8034",
            "ssh_host": None,
        }
        base.update(over)
        return base

    def test_matching_listener_is_kept(self):
        # Idempotence: attach re-runs the starter on every attach, so an
        # identical (version, server_url, ssh_host) must never churn the
        # listener -- a restart drops the keyboard hook for a moment.
        assert (
            hotkey_restart_reason(self._manifest(), "http://127.0.0.1:8034", None)
            is None
        )

    def test_missing_manifest_is_stale(self):
        # Every pre-3.6.0 listener wrote no manifest at all; it cannot be
        # vouched for, so it gets replaced.
        reason = hotkey_restart_reason(None, "http://127.0.0.1:8034", None)
        assert reason is not None
        assert "manifest" in reason

    def test_version_skew_restarts(self):
        # The pip-upgrade bug: the OLD process keeps running OLD code (no F2
        # handler at all in some versions) until someone hand-kills it.
        reason = hotkey_restart_reason(
            self._manifest(version="3.5.0"), "http://127.0.0.1:8034", None
        )
        assert reason is not None
        assert "version skew" in reason and "3.5.0" in reason

    def test_server_url_change_restarts(self):
        # A listener wired to loopback by a local launch cannot serve the host
        # tailnet URL `magent attach` wants.
        reason = hotkey_restart_reason(
            self._manifest(), "http://host.tailnet:8034", None
        )
        assert reason is not None
        assert "server_url" in reason

    def test_ssh_host_change_restarts(self):
        # Same bug, other direction: F2 must open the folder on the machine the
        # windows are actually attached to.
        reason = hotkey_restart_reason(
            self._manifest(), "http://127.0.0.1:8034", "mdssh"
        )
        assert reason is not None
        assert "ssh_host" in reason

    def test_ssh_host_dropped_restarts(self):
        reason = hotkey_restart_reason(
            self._manifest(ssh_host="mdssh"), "http://127.0.0.1:8034", None
        )
        assert reason is not None
        assert "ssh_host" in reason

    def test_unreadable_manifest_fields_are_stale(self):
        # listener_manifest() maps a corrupt/absent field to None, which must
        # not accidentally compare equal to a real target.
        reason = hotkey_restart_reason(
            {"version": None, "server_url": None, "ssh_host": None},
            "http://127.0.0.1:8034",
            None,
        )
        assert reason is not None


class TestLocalHotkeyListener:
    """A local launch starts the Alt+V / F2 listener itself. Before this, only
    `magent attach` did -- so locally the psmux status bar advertised "F2 code"
    with no live handler behind it."""

    @pytest.fixture(autouse=True)
    def _no_real_processes(self, monkeypatch):
        # The upload server would otherwise really be spawned, and tailnet.ip4
        # would really shell out to `tailscale`.
        monkeypatch.setattr("magent.launch.spawn_detached", lambda *a, **k: None)
        monkeypatch.setattr("magent.launch.tailnet.ip4", lambda: None)

    @pytest.fixture
    def spawned(self, monkeypatch):
        """Intercept the detached spawn: the real one needs a Windows-only
        hotkey import and would leave a keyboard hook running."""
        calls: list[tuple[str, str | None]] = []

        def _fake(server_url, ssh_host=None):
            calls.append((server_url, ssh_host))
            return 4242  # the child came up and wrote its pid

        monkeypatch.setattr("magent.launch.start_hotkey_listener", _fake)
        return calls

    def _run(self, fp, spawned, **opts):
        cfg = MagentConfig(
            projects=[],
            settings=Settings(psmux=True, upload_server=True, upload_port=9911),
        )
        result = _LaunchResult(
            targets=[],
            psmux_windows=[
                PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude")
            ],
            psmux_colors={"a": None},
        )
        _start_psmux_and_upload(fp, cfg, RunOpts(**opts), result)

    def test_starts_the_listener_pointed_at_loopback(self, spawned, capsys):
        fp = FakePlatform(supports_psmux=True, supports_hotkey=True)

        self._run(fp, spawned)

        # Loopback, never the tailnet IP: a local listener must not depend on
        # Tailscale being up to reach its own upload server.
        assert spawned == [("http://127.0.0.1:9911", None)]
        out = capsys.readouterr().out
        assert "hotkey listener" in out
        assert "Alt+V" in out and "F2" in out

    def test_skipped_when_the_platform_has_no_hotkey_support(self, spawned, capsys):
        fp = FakePlatform(supports_psmux=True, supports_hotkey=False)

        self._run(fp, spawned)

        assert spawned == []
        assert "hotkey listener" not in capsys.readouterr().out

    def test_skipped_without_an_upload_server(self, spawned, capsys):
        # F2 resolves its folder through the server's /api/sessions -- with no
        # server there is nothing for the listener to do.
        fp = FakePlatform(supports_psmux=True, supports_hotkey=True)
        cfg = MagentConfig(projects=[], settings=Settings(psmux=True))
        result = _LaunchResult(
            targets=[],
            psmux_windows=[
                PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude")
            ],
            psmux_colors={"a": None},
        )

        _start_psmux_and_upload(fp, cfg, RunOpts(), result)

        assert spawned == []
        assert "hotkey listener" not in capsys.readouterr().out

    def test_skipped_on_dry_run(self, spawned, capsys):
        fp = FakePlatform(supports_psmux=True, supports_hotkey=True)

        self._run(fp, spawned, dry_run=True)

        assert spawned == []
        assert "hotkey listener" not in capsys.readouterr().out

    def test_no_echo_when_the_listener_never_confirms(self, monkeypatch, capsys):
        # start_hotkey_listener returns None when the child never wrote a pid;
        # claiming it's up would be a lie.
        monkeypatch.setattr("magent.launch.start_hotkey_listener", lambda *a, **k: None)
        fp = FakePlatform(supports_psmux=True, supports_hotkey=True)

        self._run(fp, None)

        assert "hotkey listener" not in capsys.readouterr().out


class TestLaunchProjects:
    """Direct unit tests for the extracted per-project dispatch loop (R4,
    Step 4), which returns the typed _LaunchResult the downstream phases
    consume."""

    def test_builds_targets_and_psmux(self, tmp_path, fake_sleep):
        fp = FakePlatform(supports_psmux=True)
        projects = [ProjectConfig(path=str(tmp_path), tool="claude", title="proj")]
        cfg = MagentConfig(
            projects=projects,
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        result = _launch_projects(fp, cfg, RunOpts(), projects, None)

        assert result.targets == [
            _Target(name="proj", key="proj", mode="magent-name", is_new=True)
        ]
        assert len(result.psmux_windows) == 1
        assert result.psmux_windows[0].window_name == "proj"

    def test_ide_populates_targets(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        projects = [ProjectConfig(path=str(tmp_path), tool="code")]
        cfg = MagentConfig(
            projects=projects,
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        result = _launch_projects(fp, cfg, RunOpts(), projects, None)

        assert len(result.targets) == 1
        assert result.targets[0].mode == "contains"
        assert result.targets[0].is_new is True
        assert len(fp.launched_vscode) == 1


class TestPrepareGrid:
    """Direct unit tests for the extracted grid phase (R4, Step 5)."""

    def test_returns_none_without_monitors(self, capsys):
        fp = FakePlatform(monitors=[])
        cfg = MagentConfig(projects=[])

        result = _prepare_grid(fp, cfg, RunOpts())

        assert result is None
        assert fp.dpi_aware_calls == 1
        # the no-monitors echo/log stays in the shell, not in this phase
        assert capsys.readouterr().out == ""

    def test_returns_slots(self, capsys):
        fp = FakePlatform()
        cfg = MagentConfig(projects=[])

        result = _prepare_grid(fp, cfg, RunOpts())

        assert result is not None
        assert len(result) > 0
        assert "screen(s)" in capsys.readouterr().out


class TestSelectProjects:
    """Direct unit tests for the extracted project-selection phase (R4, Step 6)."""

    def test_filters_group(self):
        cfg = MagentConfig(
            projects=[
                ProjectConfig(path="/a", group="a"),
                ProjectConfig(path="/b", group="b"),
            ]
        )

        result = _select_projects(cfg, RunOpts(group="a"))

        assert result is not None
        assert [p.path for p in result] == ["/a"]

    def test_empty_group_returns_none(self, capsys):
        cfg = MagentConfig(projects=[ProjectConfig(path="/a", group="a")])

        result = _select_projects(cfg, RunOpts(group="nope"))

        assert result is None
        err = capsys.readouterr().err
        assert "No projects in group" in err
        assert "a" in err


class TestDispatchIdeProject:
    """Direct unit tests for the IDE-branch dispatch helper (R4, C7) split
    out of _launch_projects along the seam the loop already marked with
    `continue`. targets is a caller-owned accumulator appended to in place;
    the new_count delta is returned (ints can't be mutated via a parameter)."""

    def test_launches_and_returns_delta_one(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="code")
        cfg = MagentConfig(projects=[proj])
        targets: list[_Target] = []

        delta = _dispatch_ide_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "code",
            False,
            None,
            lambda key, mode: False,
            targets,
        )

        assert delta == 1
        assert len(targets) == 1
        assert targets[0].mode == "contains"
        assert targets[0].is_new is True
        assert len(fp.launched_vscode) == 1
        assert fp.launched_vscode[0].command == "code"

    def test_running_skips_launch_and_returns_delta_zero(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="code")
        cfg = MagentConfig(projects=[proj])
        targets: list[_Target] = []

        delta = _dispatch_ide_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "code",
            False,
            None,
            lambda key, mode: True,
            targets,
        )

        assert delta == 0
        assert len(targets) == 1
        assert targets[0].is_new is False
        assert fp.launched_vscode == []


class TestDispatchCliAgentProject:
    """Direct unit tests for the CLI-agent-branch dispatch helper (R4, C7).
    E4's capability sites (_get_session_ids/_wrap_happy/multi-window gate)
    live here verbatim. targets/psmux_windows/psmux_colors are caller-owned
    accumulators appended to in place; new_count is returned."""

    def test_launches_terminal_and_returns_delta(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj], settings=Settings(tools={"claude": "claude --continue"})
        )
        targets: list[_Target] = []
        psmux_windows: list[PsmuxWindowOpts] = []
        psmux_colors: dict[str, str | None] = {}

        delta = _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            targets,
            psmux_windows,
            psmux_colors,
        )

        assert delta == 1
        assert len(fp.launched_terminals) == 1
        assert fp.launched_terminals[0].title == "magent:proj"
        assert targets == [
            _Target(name="proj", key="proj", mode="magent-name", is_new=True)
        ]
        assert psmux_windows == []

    def test_psmux_collects_instead_of_launching(self, tmp_path, fake_sleep):
        fp = FakePlatform(supports_psmux=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(tools={"claude": "claude --continue"}, psmux=True),
        )
        targets: list[_Target] = []
        psmux_windows: list[PsmuxWindowOpts] = []
        psmux_colors: dict[str, str | None] = {}

        delta = _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            True,
            lambda key, mode: False,
            targets,
            psmux_windows,
            psmux_colors,
        )

        assert delta == 1
        assert fp.launched_terminals == []
        assert len(psmux_windows) == 1
        assert psmux_windows[0].window_name == "proj"

    def test_unknown_tool_skips_and_returns_delta_zero(
        self, tmp_path, fake_sleep, capsys
    ):
        proj = ProjectConfig(path=str(tmp_path), tool="ghost-tool", title="proj")
        cfg = MagentConfig(
            projects=[proj], settings=Settings(tools={"claude": "claude --continue"})
        )
        targets: list[_Target] = []

        delta = _dispatch_cli_agent_project(
            FakePlatform(),
            cfg,
            RunOpts(),
            proj,
            "ghost-tool",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            targets,
            [],
            {},
        )

        assert delta == 0
        assert targets == []
        assert "unknown tool" in capsys.readouterr().out


class TestWindowTitlePrefixDisabled:
    """windowTitlePrefix=false: produced titles are bare project names and the
    launcher tiles by exact-title match instead of the magent-name grammar
    (the launcher knows the exact title it set, so it needs no prefix to
    resolve the window)."""

    def test_bare_title_and_exact_tiling(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(
                tools={"claude": "claude --continue"}, window_title_prefix=False
            ),
        )
        targets: list[_Target] = []

        _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            targets,
            [],
            {},
        )

        # No magent: prefix on the launched terminal title.
        assert fp.launched_terminals[0].title == "proj"
        # Tiling target uses exact-title match on the bare name.
        assert targets == [_Target(name="proj", key="proj", mode="exact", is_new=True)]

    def test_running_check_uses_exact_mode(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(
                tools={"claude": "claude --continue"}, window_title_prefix=False
            ),
        )
        seen: list[tuple[str, str]] = []

        _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: (seen.append((key, mode)), False)[1],
            [],
            [],
            {},
        )

        # The already-running probe must query by exact bare title, not the
        # magent-name grammar (which would never match a bare-titled window).
        assert seen == [("proj", "exact")]


class TestFreshStartInANewProjectDirectory:
    """`claude --continue` resumes the CWD's most recent conversation. In a
    project directory that never hosted one -- one just added to magent, a
    fresh machine, a cleaned ~/.claude/projects -- there is nothing to
    continue: claude errors out, the pane is left at a dead shell, the agent
    never starts, and every revive re-runs the same failing command. The
    single-window path is where the configured command reaches such a
    directory verbatim (multi-window already routes through
    build_resume_command, which strips the flag when it has no session id)."""

    def _dispatch(self, monkeypatch, proj, cfg, *, has_session, is_remote=False):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session",
            lambda project_dir, home_override=None: has_session,
        )
        fp = FakePlatform()
        _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            proj.tool or "claude",
            is_remote,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            [],
            [],
            {},
        )
        return fp.launched_terminals[0].command

    def _cfg(self, proj, **tools):
        return MagentConfig(
            projects=[proj],
            settings=Settings(tools=tools or {"claude": "claude --continue"}),
        )

    def test_no_prior_conversation_drops_the_continue_flag(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cmd = self._dispatch(monkeypatch, proj, self._cfg(proj), has_session=False)
        assert cmd == "claude"

    def test_a_prior_conversation_is_still_continued(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cmd = self._dispatch(monkeypatch, proj, self._cfg(proj), has_session=True)
        assert cmd == "claude --continue"

    def test_a_remote_project_is_never_decided_from_the_local_store(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        # The command runs on the far host: this machine's ~/.claude has no
        # bearing on whether that host has a conversation to continue.
        proj = ProjectConfig(
            path=str(tmp_path), tool="claude", title="proj", host="deck"
        )
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session",
            lambda project_dir, home_override=None: pytest.fail(
                "probed the local session store for a remote project"
            ),
        )
        fp = FakePlatform()
        _dispatch_cli_agent_project(
            fp,
            self._cfg(proj),
            RunOpts(),
            proj,
            "claude",
            True,
            None,
            self._cfg(proj).settings.tools,
            False,
            lambda key, mode: False,
            [],
            [],
            {},
        )
        assert fp.launched_terminals[0].command == "claude --continue"

    def test_an_explicit_per_window_command_is_never_rewritten(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        # windows[i].command is the user's literal command line. Even in a
        # directory with no conversation, magent runs exactly what it says.
        proj = ProjectConfig(
            path=str(tmp_path),
            tool="claude",
            title="proj",
            windows=[WindowConfig(command="claude --continue --verbose")],
        )
        cmd = self._dispatch(monkeypatch, proj, self._cfg(proj), has_session=False)
        assert cmd == "claude --continue --verbose"

    def test_a_per_window_tool_override_gets_the_same_treatment(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        # One window, overridden to a tool whose configured command carries
        # the flag: the override runs through the same probe as the base tool.
        proj = ProjectConfig(
            path=str(tmp_path),
            tool="codex",
            title="proj",
            windows=[WindowConfig(tool="claude")],
        )
        cfg = self._cfg(proj, codex="codex", claude="claude --continue")
        cmd = self._dispatch(monkeypatch, proj, cfg, has_session=False)
        assert cmd == "claude"

    def test_a_tool_with_no_resume_flag_is_untouched(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        proj = ProjectConfig(path=str(tmp_path), tool="codex", title="proj")
        cmd = self._dispatch(
            monkeypatch, proj, self._cfg(proj, codex="codex"), has_session=False
        )
        assert cmd == "codex"


class TestPerWindowToolOverride:
    """Regression pins for the per-window ``tool`` override (P3-06 follow-up).

    Bug: resumable session ids are discovered ONCE for the project's base
    tool, but a per-window override (``windows[i].tool``) was still handed
    ``session_ids[i]`` -- so a base-claude project whose window 2 overrides to
    codex launched it as ``codex resume <claude-uuid>``, a foreign session id.
    An override window must never borrow the base tool's session ids, and an
    override naming a tool absent from ``settings.tools`` must warn and fall
    back to the base tool entirely (not silently run base_cmd while logging
    the bogus name)."""

    def _dispatch(self, fp, cfg, proj, tool, monkeypatch, session_ids):
        # Fake session discovery: the base tool's ids, one per window.
        monkeypatch.setattr(
            "magent.launch._get_session_ids",
            lambda _tool, _dir, _count: list(session_ids),
        )
        targets: list[_Target] = []
        return _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            tool,
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            targets,
            [],
            {},
        )

    def test_override_window_does_not_reuse_base_tool_session_id(
        self, tmp_path, fake_sleep, monkeypatch
    ):
        fp = FakePlatform()
        proj = ProjectConfig(
            path=str(tmp_path),
            tool="claude",
            title="proj",
            windows=[WindowConfig(), WindowConfig(tool="codex")],
        )
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(
                tools={"claude": "claude --continue", "codex": "codex"},
                default_tool="claude",
            ),
        )

        self._dispatch(
            fp,
            cfg,
            proj,
            "claude",
            monkeypatch,
            session_ids=["claude-sid-AAA", "claude-sid-BBB"],
        )

        # Window 1 (base claude): resumed on its OWN discovered session id.
        assert fp.launched_terminals[0].command == "claude --resume claude-sid-AAA"
        # Window 2 (codex override): fresh codex form, and crucially NO claude
        # session id anywhere in it (the bug shipped `codex resume <claude-uuid>`).
        assert fp.launched_terminals[1].command == "codex"
        assert "claude-sid" not in fp.launched_terminals[1].command

    def test_unknown_override_tool_warns_and_falls_back_to_base(
        self, tmp_path, fake_sleep, monkeypatch, capsys
    ):
        fp = FakePlatform()
        proj = ProjectConfig(
            path=str(tmp_path),
            tool="claude",
            title="proj",
            windows=[WindowConfig(tool="nope"), WindowConfig()],
        )
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude"
            ),
        )

        self._dispatch(fp, cfg, proj, "claude", monkeypatch, session_ids=[None, None])

        out = capsys.readouterr().out
        assert "WARN:" in out
        assert "unknown tool 'nope'" in out
        assert "windows[0]" in out
        assert "using 'claude'" in out
        # The window runs the base tool (claude), never the bogus 'nope'.
        assert fp.launched_terminals[0].command == "claude"
        assert "nope" not in fp.launched_terminals[0].command


class TestPsmuxWindowDedupe:
    """Regression pins for the duplicate-window bug: a user with windows open
    picked menu option 2 ("Re-tile all open windows") and got a second window
    per already-open psmux session.

    Cause: the psmux collection block ran BEFORE (and independently of) the
    `is_running` probe, and every collected window gets an `attach_psmux` --
    which is `wt -w new ... psmux attach`, a brand-new window with no dedupe of
    its own. `launch_psmux_session`'s `has-session` probe only dedupes
    SESSIONS. The non-psmux path was already gated on `not running`; these pin
    the psmux path to the same window-level rule (the attach path's v3.4.0
    dedupe, `tiling.window_open` in cli/attach.py, is the precedent)."""

    def test_open_window_is_not_collected_or_attached(
        self, monkeypatch, tmp_path, fake_sleep
    ):
        fp = FakePlatform(supports_psmux=True, windows={"magent:proj": 555})
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        # Nothing collected => _start_psmux_and_upload no-ops entirely: no
        # session create, no attach, so the live session is left untouched.
        assert fp.launched_psmux == []
        assert fp.attached_psmux == []
        assert fp.launched_terminals == []

    def test_closed_window_is_still_collected(self, monkeypatch, tmp_path, fake_sleep):
        # The other half of the three-way rule: window closed => collected, so
        # attach reopens a window (onto the live session when has-session
        # answers, onto a freshly created one when it doesn't).
        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[ProjectConfig(path=str(tmp_path), tool="claude", title="proj")],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        assert [w.window_name for w in fp.launched_psmux] == ["proj"]
        assert len(fp.attached_psmux) == 1

    def test_only_the_closed_window_of_a_mixed_fleet_is_collected(
        self, monkeypatch, tmp_path, fake_sleep
    ):
        open_dir = tmp_path / "alpha"
        open_dir.mkdir()
        closed_dir = tmp_path / "beta"
        closed_dir.mkdir()
        fp = FakePlatform(supports_psmux=True, windows={"magent:alpha": 555})
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[
                ProjectConfig(path=str(open_dir), tool="claude", title="alpha"),
                ProjectConfig(path=str(closed_dir), tool="claude", title="beta"),
            ],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        rc = run_magent(cfg, RunOpts(retile_all=True))

        assert rc == 0
        assert [w.window_name for w in fp.launched_psmux] == ["beta"]
        assert [a[0] for a in fp.attached_psmux] == ["beta"]
        # Both windows are still tiling targets -- retile_all places the open
        # one too; only the duplicate SPAWN is suppressed.
        assert (555, Rect(x=0, y=0, w=960, h=1080)) in fp.moved

    def test_dispatch_skips_collection_when_running(self, tmp_path, fake_sleep):
        fp = FakePlatform(supports_psmux=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(tools={"claude": "claude --continue"}, psmux=True),
        )
        targets: list[_Target] = []
        psmux_windows: list[PsmuxWindowOpts] = []
        psmux_colors: dict[str, str | None] = {}

        delta = _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            True,
            lambda key, mode: True,
            targets,
            psmux_windows,
            psmux_colors,
        )

        assert delta == 0
        assert psmux_windows == []
        assert psmux_colors == {}
        assert fp.launched_terminals == []
        # The window is still a tiling target, flagged as already-open.
        assert targets == [
            _Target(name="proj", key="proj", mode="magent-name", is_new=False)
        ]


class TestTileOnly:
    """`RunOpts.tile_only`: build every tiling target but launch nothing.

    Menu option 2 and a bare `--retile-all` promise a re-tile; before this they
    ran the whole launch phase, so a window the user had just closed came back
    (terminal path) or was collected and attached (psmux path)."""

    def test_no_terminal_launched(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj], settings=Settings(tools={"claude": "claude --continue"})
        )
        targets: list[_Target] = []

        _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(retile_all=True, tile_only=True),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            False,
            lambda key, mode: False,
            targets,
            [],
            {},
        )

        assert fp.launched_terminals == []
        # The target is still built, so _tile_targets can place it if it IS
        # open; a closed window simply reports "not found".
        assert targets == [
            _Target(name="proj", key="proj", mode="magent-name", is_new=True)
        ]

    def test_no_psmux_collection(self, tmp_path, fake_sleep):
        fp = FakePlatform(supports_psmux=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(
            projects=[proj],
            settings=Settings(tools={"claude": "claude --continue"}, psmux=True),
        )
        targets: list[_Target] = []
        psmux_windows: list[PsmuxWindowOpts] = []

        _dispatch_cli_agent_project(
            fp,
            cfg,
            RunOpts(retile_all=True, tile_only=True),
            proj,
            "claude",
            False,
            None,
            cfg.settings.tools,
            True,
            lambda key, mode: False,
            targets,
            psmux_windows,
            {},
        )

        assert psmux_windows == []
        assert len(targets) == 1

    def test_no_vscode_launched(self, tmp_path, fake_sleep):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="code")
        cfg = MagentConfig(projects=[proj])
        targets: list[_Target] = []

        delta = _dispatch_ide_project(
            fp,
            cfg,
            RunOpts(retile_all=True, tile_only=True),
            proj,
            "code",
            False,
            None,
            lambda key, mode: False,
            targets,
        )

        assert fp.launched_vscode == []
        assert delta == 1  # bookkeeping unchanged; only the spawn is skipped
        assert len(targets) == 1

    def test_run_magent_tiles_open_windows_and_launches_nothing(
        self, monkeypatch, tmp_path, fake_sleep
    ):
        open_dir = tmp_path / "alpha"
        open_dir.mkdir()
        closed_dir = tmp_path / "beta"
        closed_dir.mkdir()
        fp = FakePlatform(supports_psmux=True, windows={"magent:alpha": 555})
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        cfg = MagentConfig(
            projects=[
                ProjectConfig(path=str(open_dir), tool="claude", title="alpha"),
                ProjectConfig(path=str(closed_dir), tool="claude", title="beta"),
            ],
            settings=Settings(
                tools={"claude": "claude --continue"}, default_tool="claude", psmux=True
            ),
        )

        rc = run_magent(cfg, RunOpts(retile_all=True, tile_only=True))

        assert rc == 0
        assert fp.launched_terminals == []
        assert fp.launched_vscode == []
        assert fp.launched_psmux == []
        assert fp.attached_psmux == []
        # The open window still gets re-tiled; the closed one stays closed.
        assert fp.moved == [(555, Rect(x=0, y=0, w=960, h=1080))]


class TestBaseDirExpansion:
    """Characterization pin (P1-08), written before the _expand_base_dir
    extraction: a configured base_dir gets env vars expanded and forward
    slashes normalized to os.sep before project paths resolve against it."""

    def test_expand_base_dir_normalizes_env_and_separators(self, monkeypatch):
        monkeypatch.setenv("MD_TEST_BASE", "base")
        assert _expand_base_dir("$MD_TEST_BASE/x") == "base" + os.sep + "x"

    def test_expand_base_dir_expands_user(self):
        assert not _expand_base_dir("~/x").startswith("~")

    def test_eligible_psmux_projects_expands_base_dir(self, tmp_path, monkeypatch):
        proj_dir = tmp_path / "sub" / "proj"
        proj_dir.mkdir(parents=True)
        monkeypatch.setenv("MD_TEST_BASE", str(tmp_path))
        cfg = MagentConfig(
            projects=[ProjectConfig(path="proj")],
            base_dir="$MD_TEST_BASE/sub",
        )

        out = eligible_psmux_projects(cfg)

        assert len(out) == 1
        assert out[0]["resolved"] == str(proj_dir)
