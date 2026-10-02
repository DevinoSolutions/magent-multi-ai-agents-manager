"""The launch path and the tab icons: which windows get a profile, under which
key, and when the answer is "no profile" (so the tab opens exactly as it did
before ``wt_profiles`` existed)."""

from __future__ import annotations

import pytest

from magent import launch, wt_profiles
from magent.config import MagentConfig, ProjectConfig, Settings
from magent.launch import (
    RunOpts,
    _bring_up_node_windows,
    _dispatch_cli_agent_project,
    _ensure_tab_profile,
    _LaunchResult,
    _start_psmux_and_upload,
    _Target,
)
from magent.platform import PsmuxWindowOpts
from tests.conftest import FakePlatform


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)


@pytest.fixture
def icons_on(monkeypatch):
    monkeypatch.setenv("MAGENT_WT_ICONS", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


def _manifest_names() -> list[str]:
    return [p.name for p in wt_profiles.read_fragment()]


def _dispatch(fp, proj, cfg, *, psmux=False, opts=None, base_dir=None):
    targets: list[_Target] = []
    windows: list[PsmuxWindowOpts] = []
    colors: dict[str, str | None] = {}
    _dispatch_cli_agent_project(
        fp,
        cfg,
        opts or RunOpts(),
        proj,
        "claude",
        bool(proj.host),
        base_dir,
        cfg.settings.tools,
        psmux,
        lambda key, mode: False,
        targets,
        windows,
        colors,
    )
    return targets, windows, colors


class TestEnsureTabProfile:
    def _call(self, fp, cfg, proj, key="alpha", project_dir=None):
        return _ensure_tab_profile(fp, cfg, key, key, proj, project_dir)

    def test_none_where_the_platform_has_no_windows_terminal(self, icons_on):
        proj = ProjectConfig(path="alpha", color="#a855f7")
        cfg = MagentConfig(projects=[proj])
        assert self._call(FakePlatform(), cfg, proj) is None
        assert _manifest_names() == []

    def test_none_when_the_setting_is_off(self, icons_on):
        proj = ProjectConfig(path="alpha", color="#a855f7")
        cfg = MagentConfig(projects=[proj], settings=Settings(terminal_icons=False))
        fp = FakePlatform(supports_wt_profiles=True)
        assert self._call(fp, cfg, proj) is None
        assert _manifest_names() == []

    def test_none_under_the_env_kill_switch(self, monkeypatch):
        # conftest already pins MAGENT_WT_ICONS=0: nothing is written.
        proj = ProjectConfig(path="alpha", color="#a855f7")
        cfg = MagentConfig(projects=[proj])
        fp = FakePlatform(supports_wt_profiles=True)
        assert self._call(fp, cfg, proj) is None
        assert _manifest_names() == []

    def test_writes_the_profile_and_names_it(self, icons_on):
        proj = ProjectConfig(path="alpha", color="#a855f7")
        cfg = MagentConfig(projects=[proj])
        fp = FakePlatform(supports_wt_profiles=True)
        assert self._call(fp, cfg, proj) == "magent: alpha"
        assert _manifest_names() == ["magent: alpha"]

    def test_the_project_icon_and_dir_reach_the_spec(self, icons_on, tmp_path):
        (tmp_path / "logo.png").write_bytes(
            wt_profiles.icons._png(32, 32, b"\x00\x80\xff\xff" * 32 * 32)
        )
        proj = ProjectConfig(path=str(tmp_path), color="#a855f7", icon="logo.png")
        cfg = MagentConfig(projects=[proj])
        fp = FakePlatform(supports_wt_profiles=True)
        self._call(fp, cfg, proj, project_dir=str(tmp_path))
        (entry,) = wt_profiles.read_fragment()
        assert entry.source == "config"


class TestTerminalLaunches:
    def test_a_local_terminal_gets_the_profile_for_its_title(self, icons_on, tmp_path):
        fp = FakePlatform(supports_wt_profiles=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(projects=[proj])
        _dispatch(fp, proj, cfg)
        (opts,) = fp.launched_terminals
        assert opts.title == "magent:proj"
        assert opts.profile == "magent: proj"
        assert _manifest_names() == ["magent: proj"]

    def test_without_wt_support_the_opts_are_what_they_always_were(
        self, icons_on, tmp_path
    ):
        fp = FakePlatform()
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        _dispatch(fp, proj, MagentConfig(projects=[proj]))
        (opts,) = fp.launched_terminals
        assert opts.profile is None
        assert _manifest_names() == []

    def test_a_remote_project_gets_a_profile_without_a_local_dir(self, icons_on):
        fp = FakePlatform(supports_wt_profiles=True)
        proj = ProjectConfig(
            path="/srv/api", tool="claude", title="api", host="u@h", color="#112233"
        )
        _dispatch(fp, proj, MagentConfig(projects=[proj]))
        (opts,) = fp.launched_terminals
        assert opts.profile == "magent: api"
        (entry,) = wt_profiles.read_fragment()
        assert entry.source == "generated"

    def test_a_dry_run_and_a_tile_only_pass_write_nothing(self, icons_on, tmp_path):
        fp = FakePlatform(supports_wt_profiles=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(projects=[proj])
        _dispatch(fp, proj, cfg, opts=RunOpts(dry_run=True))
        _dispatch(fp, proj, cfg, opts=RunOpts(tile_only=True))
        assert fp.launched_terminals == []
        assert _manifest_names() == []

    def test_a_logo_the_repo_ships_wins_over_the_badge(self, icons_on, tmp_path):
        (tmp_path / "favicon.png").write_bytes(
            wt_profiles.icons._png(32, 32, b"\xff\x00\x00\xff" * 32 * 32)
        )
        fp = FakePlatform(supports_wt_profiles=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        _dispatch(fp, proj, MagentConfig(projects=[proj]))
        (entry,) = wt_profiles.read_fragment()
        assert entry.source == "discovered"


class TestPsmuxWindows:
    def test_collecting_writes_the_profile_under_the_session_name(
        self, icons_on, tmp_path
    ):
        fp = FakePlatform(supports_psmux=True, supports_wt_profiles=True)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="My Proj")
        cfg = MagentConfig(projects=[proj], settings=Settings(psmux=True))
        _targets, windows, _colors = _dispatch(fp, proj, cfg, psmux=True)
        (window,) = windows
        # The sanitized session name, not the raw title: that is the window
        # key every later lookup (attach) uses.
        assert window.window_name == "My-Proj"
        assert _manifest_names() == ["magent: My-Proj"]

    def test_attach_passes_the_profile_when_the_fragment_has_it(self, icons_on):
        wt_profiles.sync([wt_profiles.IconSpec("a", "a", "#111111")])
        fp = FakePlatform(supports_psmux=True)
        windows = [
            PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude"),
            PsmuxWindowOpts(window_name="b", cwd="/tmp/b", command="claude"),
        ]
        result = _LaunchResult(
            targets=[], psmux_windows=windows, psmux_colors={"a": None, "b": None}
        )
        _start_psmux_and_upload(fp, MagentConfig(projects=[]), RunOpts(), result)
        assert fp.attached_profiles == ["magent: a", None]

    def test_attach_without_any_fragment_is_the_historical_call(self):
        fp = FakePlatform(supports_psmux=True)
        windows = [PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude")]
        result = _LaunchResult(
            targets=[], psmux_windows=windows, psmux_colors={"a": "#111111"}
        )
        _start_psmux_and_upload(fp, MagentConfig(projects=[]), RunOpts(), result)
        assert fp.attached_psmux == [("a", "magent:a", "#111111", None)]
        assert fp.attached_profiles == [None]


class TestNodeWindows:
    def test_the_profile_is_written_before_the_bring_up_opens_the_window(
        self, icons_on, monkeypatch
    ):
        from magent import nodes

        proj = ProjectConfig(path="/p/api", node="n1", color="#336699")
        sid = nodes.node_sid(proj)
        seen: list[list[str]] = []

        def fake_run(config, projects, *, allow_dirty, window):
            # What spawn_attach_window would see at this moment.
            seen.append(_manifest_names())
            return []

        monkeypatch.setattr(launch, "_run_node_bring_ups", fake_run)
        monkeypatch.setattr(
            launch, "_warn_node_windows_will_not_reconnect", lambda p: None
        )
        fp = FakePlatform(supports_wt_profiles=True)
        result = _LaunchResult(
            targets=[], psmux_windows=[], psmux_colors={}, node_projects=(proj,)
        )
        _bring_up_node_windows(fp, MagentConfig(projects=[proj]), RunOpts(), result)
        assert seen == [[f"magent: {sid}"]]


class TestTurningTheSettingOff:
    def test_run_removes_a_fragment_left_from_before(
        self, icons_on, monkeypatch, tmp_path
    ):
        wt_profiles.sync([wt_profiles.IconSpec("old", "old", "#111111")])
        assert _manifest_names() == ["magent: old"]
        fp = FakePlatform(supports_wt_profiles=True)
        monkeypatch.setattr(launch, "get_platform", lambda: fp)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(projects=[proj], settings=Settings(terminal_icons=False))
        launch.run_magent(cfg, RunOpts(config_path=""))
        assert _manifest_names() == []
        assert fp.launched_terminals[0].profile is None

    def test_a_dry_run_leaves_the_fragment_alone(self, icons_on, monkeypatch, tmp_path):
        wt_profiles.sync([wt_profiles.IconSpec("old", "old", "#111111")])
        fp = FakePlatform(supports_wt_profiles=True)
        monkeypatch.setattr(launch, "get_platform", lambda: fp)
        proj = ProjectConfig(path=str(tmp_path), tool="claude", title="proj")
        cfg = MagentConfig(projects=[proj], settings=Settings(terminal_icons=False))
        launch.run_magent(cfg, RunOpts(config_path="", dry_run=True))
        assert _manifest_names() == ["magent: old"]
