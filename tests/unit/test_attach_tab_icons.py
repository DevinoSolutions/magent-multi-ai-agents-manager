"""`magent attach` and the tab icons: the fragment gets a profile for every
window about to open, before the `wt` that names it is spawned -- and an opt-out
or a platform without Windows Terminal leaves the spawn exactly as it was."""

from __future__ import annotations

import json

import pytest

from magent import wt_profiles
from magent.cli import attach as attach_mod
from tests.conftest import FakePlatform


@pytest.fixture
def icons_on(monkeypatch):
    monkeypatch.setenv("MAGENT_WT_ICONS", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


def _platform(monkeypatch, *, wt: bool, windows=None) -> FakePlatform:
    fp = FakePlatform(windows=dict(windows or {}), supports_wt_profiles=wt)
    monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
    return fp


def _names() -> list[str]:
    return [p.name for p in wt_profiles.read_fragment()]


def _config(tmp_path, **settings) -> str:
    path = tmp_path / "cfg.json"
    path.write_text(
        json.dumps({"version": 4, "projects": [], "settings": settings}),
        encoding="utf-8",
    )
    return str(path)


class TestEnsureAttachProfiles:
    def test_writes_a_profile_per_session(self, monkeypatch, icons_on):
        _platform(monkeypatch, wt=True)
        attach_mod._ensure_attach_profiles(["api", "web"], None)
        assert sorted(_names()) == ["magent: api", "magent: web"]

    def test_the_remote_session_gets_the_generated_badge(self, monkeypatch, icons_on):
        _platform(monkeypatch, wt=True)
        attach_mod._ensure_attach_profiles(["api"], None)
        (entry,) = wt_profiles.read_fragment()
        assert entry.source == "generated"

    def test_nothing_without_windows_terminal(self, monkeypatch, icons_on):
        _platform(monkeypatch, wt=False)
        attach_mod._ensure_attach_profiles(["api"], None)
        assert _names() == []

    def test_nothing_under_the_kill_switch(self, monkeypatch):
        # conftest pins MAGENT_WT_ICONS=0.
        _platform(monkeypatch, wt=True)
        attach_mod._ensure_attach_profiles(["api"], None)
        assert _names() == []

    def test_the_setting_off_writes_nothing(self, monkeypatch, icons_on, tmp_path):
        _platform(monkeypatch, wt=True)
        attach_mod._ensure_attach_profiles(
            ["api"], _config(tmp_path, terminalIcons=False)
        )
        assert _names() == []

    def test_the_setting_off_removes_a_stale_fragment_like_run_magent_does(
        self, monkeypatch, icons_on, tmp_path
    ):
        _platform(monkeypatch, wt=True)
        wt_profiles.sync([wt_profiles.IconSpec("old", "old", "#111111")])
        attach_mod._ensure_attach_profiles(
            ["api"], _config(tmp_path, terminalIcons=False)
        )
        assert _names() == []
        assert wt_profiles.profile_for("old") is None

    def test_a_host_session_named_like_a_local_project_does_not_take_its_icon(
        self, monkeypatch, icons_on
    ):
        _platform(monkeypatch, wt=True)
        wt_profiles.sync([wt_profiles.IconSpec("api", "Api", "#a855f7")])
        directory = wt_profiles.fragment_dir()
        before = (directory / wt_profiles.FRAGMENT_FILE).read_bytes()
        icon = wt_profiles.read_fragment()[0].icon
        for _ in range(3):
            attach_mod._ensure_attach_profiles(["api"], None)
            wt_profiles.sync([wt_profiles.IconSpec("api", "Api", "#a855f7")])
        assert (directory / wt_profiles.FRAGMENT_FILE).read_bytes() == before
        assert wt_profiles.read_fragment()[0].icon == icon
        assert wt_profiles.read_fragment()[0].source == "generated"

    def test_the_whole_batch_is_one_sync(self, monkeypatch, icons_on):
        _platform(monkeypatch, wt=True)
        calls: list[int] = []
        real = wt_profiles.sync

        def spy(specs, **kw):
            calls.append(len(specs))
            return real(specs, **kw)

        monkeypatch.setattr(wt_profiles, "sync", spy)
        attach_mod._ensure_attach_profiles(["a", "b", "c", "d"], None)
        assert calls == [4]

    def test_an_unreadable_config_is_no_opinion(self, monkeypatch, icons_on, tmp_path):
        _platform(monkeypatch, wt=True)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        attach_mod._ensure_attach_profiles(["api"], str(bad))
        assert _names() == ["magent: api"]

    def test_no_sessions_is_a_no_op(self, monkeypatch, icons_on):
        _platform(monkeypatch, wt=True)
        attach_mod._ensure_attach_profiles([], None)
        assert _names() == []


class _Proc:
    def wait(self, timeout=None):
        return 0


class TestTheFlowOpensTabsWithTheirProfile:
    def _run(
        self, monkeypatch, *, windows=None, no_mux=False, wt=True, config_path=None
    ):
        status = {
            "up": [{"name": "api", "session": "api"}],
            "down": [],
            "projects": [{"name": "api", "session": "api", "path": "/srv/api"}],
        }
        monkeypatch.setattr(
            attach_mod, "_query_status", lambda *a, **k: (status, 0, "")
        )
        monkeypatch.setattr(attach_mod, "_ssh_capture", lambda *a, **k: (0, "", ""))
        spawns: list[list[str]] = []

        def fake_popen(args, **_k):
            if args and args[0] == "wt":
                spawns.append(list(args))
            return _Proc()

        monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod, "_maybe_start_hotkey", lambda url: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(attach_mod, "_remember_last_host", lambda target: None)
        _platform(monkeypatch, wt=wt, windows=windows)
        attach_mod._attach_flow(
            "user@host", no_mux=no_mux, group=None, yes=True, config_path=config_path
        )
        return spawns

    def test_a_supervised_pane_gets_p(self, monkeypatch, icons_on):
        (argv,) = self._run(monkeypatch)
        assert argv[argv.index("-p") + 1] == "magent: api"
        assert argv.index("-p") < argv.index("--")

    def test_a_no_mux_pane_gets_p(self, monkeypatch, icons_on):
        (argv,) = self._run(monkeypatch, no_mux=True)
        assert argv[argv.index("-p") + 1] == "magent: api"

    def test_without_wt_support_the_pane_is_unchanged(self, monkeypatch, icons_on):
        (argv,) = self._run(monkeypatch, wt=False)
        assert "-p" not in argv

    @pytest.mark.parametrize("no_mux", [False, True])
    def test_the_setting_off_passes_no_p_even_with_a_stale_fragment(
        self, monkeypatch, icons_on, tmp_path, no_mux
    ):
        wt_profiles.sync([wt_profiles.IconSpec("api", "api", "#111111")])
        assert wt_profiles.profile_for("api") == "magent: api"
        (argv,) = self._run(
            monkeypatch,
            no_mux=no_mux,
            config_path=_config(tmp_path, terminalIcons=False),
        )
        assert "-p" not in argv
        assert _names() == []

    def test_an_already_open_window_writes_nothing(self, monkeypatch, icons_on):
        spawns = self._run(monkeypatch, windows={"magent:api": 1})
        assert spawns == []
        assert _names() == []
