"""The four `wt` spawn sites and the tab-icon profile.

Characterization first: with no profile on disk every argv is byte-for-byte what
it was before ``wt_profiles`` existed (the existing attach/platform pins hold
the same line from the other side). Then the one addition -- ``-p <name>``,
placed before the ``--`` that ends wt's own options, and ONLY when the profile
is really in the fragment."""

from __future__ import annotations

import sys

import pytest

from magent import attach_client, wt_profiles
from magent.platform import TerminalLaunchOpts
from magent.wt_profiles import IconSpec
from tests.conftest import FakePlatform


@pytest.fixture
def icons_on(monkeypatch):
    monkeypatch.setenv("MAGENT_WT_ICONS", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


class _Proc:
    def wait(self, timeout=None):
        return 0


def _capture(monkeypatch, module) -> list[list[str]]:
    spawns: list[list[str]] = []

    def fake_popen(args, **_k):
        spawns.append(list(args))
        return _Proc()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    return spawns


class TestAttachWindow:
    def _spawn(self, monkeypatch) -> list[str]:
        monkeypatch.setattr(attach_client, "client_exe", lambda: None)
        spawns = _capture(monkeypatch, attach_client)
        attach_client.spawn_attach_window("u@host", "api", mux="psmux")
        assert len(spawns) == 1
        return spawns[0]

    def test_no_profile_is_the_historical_argv(self, monkeypatch):
        argv = self._spawn(monkeypatch)
        assert argv[:6] == [
            "wt",
            "-w",
            "new",
            "--title",
            "magent:api",
            "--suppressApplicationTitle",
        ]
        assert argv[6] == "--"
        assert "-p" not in argv

    def test_a_synced_profile_is_passed_before_the_pane_command(
        self, monkeypatch, icons_on
    ):
        wt_profiles.sync([IconSpec("api", "api", "#a855f7")])
        argv = self._spawn(monkeypatch)
        assert argv[argv.index("-p") + 1] == "magent: api"
        assert argv.index("-p") < argv.index("--")
        # The title lock is still in the literal, next to `wt`.
        assert argv[5] == "--suppressApplicationTitle"

    def test_an_unsynced_session_gets_no_profile(self, monkeypatch, icons_on):
        wt_profiles.sync([IconSpec("web", "web", "#22c55e")])
        assert "-p" not in self._spawn(monkeypatch)

    def test_the_kill_switch_beats_a_synced_profile(self, monkeypatch, icons_on):
        wt_profiles.sync([IconSpec("api", "api", "#a855f7")])
        monkeypatch.setenv("MAGENT_WT_ICONS", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert "-p" not in self._spawn(monkeypatch)


class TestAttachNomux:
    def _run(self, monkeypatch) -> list[str]:
        from magent.cli import attach as attach_mod

        spawns = _capture(monkeypatch, attach_mod)
        monkeypatch.setattr(attach_mod, "_tile_titles", lambda titles: None)
        monkeypatch.setattr(attach_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            "magent.platform.get_platform", lambda: FakePlatform(windows={})
        )
        attach_mod._attach_nomux(
            "u@host", {"projects": [{"path": "api", "name": "api"}]}
        )
        assert len(spawns) == 1
        return spawns[0]

    def test_no_profile_is_the_historical_argv(self, monkeypatch):
        argv = self._run(monkeypatch)
        assert argv[:7] == [
            "wt",
            "-w",
            "new",
            "--title",
            "magent:api",
            "--suppressApplicationTitle",
            "--",
        ]

    def test_a_synced_profile_is_passed(self, monkeypatch, icons_on):
        wt_profiles.sync([IconSpec("api", "api", "#a855f7")])
        argv = self._run(monkeypatch)
        assert argv[argv.index("-p") + 1] == "magent: api"
        assert argv.index("-p") < argv.index("--")


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsPlatform:
    def _platform(self, monkeypatch):
        from magent.platform import windows

        spawns = _capture(monkeypatch, windows)
        return windows.WindowsPlatform(), spawns

    def test_supports_wt_profiles(self):
        from magent.platform.windows import WindowsPlatform

        assert WindowsPlatform().supports_wt_profiles() is True

    def test_launch_terminal_without_profile_is_unchanged(self, monkeypatch):
        plat, spawns = self._platform(monkeypatch)
        plat.launch_terminal(
            TerminalLaunchOpts(
                title="magent:proj", cwd="C:/p", command="claude", color="#112233"
            )
        )
        assert spawns[0] == [
            "wt",
            "-w",
            "new",
            "--suppressApplicationTitle",
            "-d",
            "C:/p",
            "--title",
            "magent:proj",
            "--tabColor",
            "#112233",
            "--",
            "cmd",
            "/k",
            "claude",
        ]

    def test_launch_terminal_with_profile_inserts_p_before_the_separator(
        self, monkeypatch
    ):
        plat, spawns = self._platform(monkeypatch)
        plat.launch_terminal(
            TerminalLaunchOpts(
                title="magent:proj",
                cwd="C:/p",
                command="claude",
                color="#112233",
                profile="magent: proj",
            )
        )
        argv = spawns[0]
        assert argv[argv.index("-p") + 1] == "magent: proj"
        assert argv.index("-p") < argv.index("--")
        assert argv[3] == "--suppressApplicationTitle"

    def test_a_profile_name_is_one_argv_element(self, monkeypatch):
        plat, spawns = self._platform(monkeypatch)
        plat.launch_terminal(
            TerminalLaunchOpts(
                title="t", cwd=".", command="claude", profile="magent: a b"
            )
        )
        assert "magent: a b" in spawns[0]

    def test_attach_psmux_with_and_without_profile(self, monkeypatch):
        from magent.platform import windows

        monkeypatch.setattr(windows, "find_psmux", lambda: "psmux.exe")
        plat, spawns = self._platform(monkeypatch)
        plat.attach_psmux("api", "magent:api", "#112233")
        plat.attach_psmux("api", "magent:api", "#112233", profile="magent: api")
        plain, profiled = spawns
        assert "-p" not in plain
        assert plain[-4:] == ["psmux.exe", "-L", "api", "attach"]
        assert profiled[profiled.index("-p") + 1] == "magent: api"
        assert profiled.index("-p") < profiled.index("--")
        assert profiled[3] == "--suppressApplicationTitle"


class TestThePlatformProbe:
    def test_abc_default_is_false(self):
        assert FakePlatform().supports_wt_profiles() is False
        assert FakePlatform(supports_wt_profiles=True).supports_wt_profiles() is True
