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
    run_magent,
)
from magent.platform import PsmuxWindowOpts, TerminalNotFoundError
from tests.conftest import FakePlatform


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
