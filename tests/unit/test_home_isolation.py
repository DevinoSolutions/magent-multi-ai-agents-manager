"""The guard that guards the guard.

``tests/conftest.py`` redirects ~ into tmp for every test and trips a tripwire
if that redirect ever stops holding. Both halves are load-bearing on a
developer machine -- a leaking test does not fail, it stops the machine's real
Alt+V listener, deletes a real lock file, and (measured) hangs 124 seconds
forwarding `magent down` over ssh to a host nobody meant to contact.

None of that damage is visible from a green suite, so the isolation itself
needs pins. These assert on the real seams the redirect uses -- a real
``Path.home()``, the real ``lockfile.exclusive_lock``, the real
``subprocess.Popen`` wrapper -- not on mocks of them.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from magent import env, lockfile, paths
from tests.conftest import (
    PLAYWRIGHT_BROWSERS_PATH,
    REAL_APPDATA,
    REAL_HOME,
    REAL_MAGENT_DIR,
    _env_points_at_real_home,
    _leaked_module_paths,
    _playwright_browsers_path,
    _tripwire_disabled,
)

# The per-test guards are a dev-machine device: CI homes are disposable and
# some CI-only tiers write them on purpose, so the tripwire stands down there
# (see _tripwire_disabled). The pins that DRIVE it stand down with it.
needs_tripwire = pytest.mark.skipif(
    _tripwire_disabled(), reason="the tripwire is disabled here (CI / opt-out)"
)


class TestTheRedirectHolds:
    def test_home_is_not_the_developers_home(self):
        assert Path.home() != REAL_HOME

    def test_the_whole_windows_home_family_moved_together(self):
        # HOME alone is a no-op on Windows: ntpath.expanduser reads USERPROFILE
        # first and falls back to HOMEDRIVE+HOMEPATH. A redirect that sets only
        # HOME looks right on Linux CI and silently does nothing on the box
        # that has a live fleet to damage -- which is how `magent down` reached
        # the real ~/.magent from a test that already "redirected HOME".
        redirected = Path.home()
        for var in ("HOME", "USERPROFILE"):
            assert Path(os.environ[var]) == redirected
        assert Path(os.environ["HOMEDRIVE"] + os.environ["HOMEPATH"]) == redirected

    def test_the_real_lockfile_lands_in_tmp(self):
        # Defect #2 exactly: TestHotkeySupervisor drove the REAL
        # `exclusive_lock`, which derives ~/.magent/<name>.lock from
        # Path.home() at CALL time -- so it took, and then UNLINKED, the lock a
        # live `magent serve` supervisor on this machine was holding.
        with lockfile.exclusive_lock("home-isolation-pin"):
            taken = Path.home() / ".magent" / "home-isolation-pin.lock"
            assert taken.exists()
            assert not (REAL_MAGENT_DIR / "home-isolation-pin.lock").exists()


win32_only = pytest.mark.skipif(
    sys.platform != "win32", reason="APPDATA is the config base on Windows only"
)


class TestAForgottenConfigFlagStaysInTmp:
    """A test that forgets ``--config`` must never find the developer's real
    config.

    With no ``--config`` and nothing in the cwd, ``paths.find_config`` falls
    back to ``env.config_base() / "magent" / "config.json"``, and on Windows
    ``config_base()`` is ``%APPDATA%``: inherited, i.e. the developer's real
    Roaming folder, where a live ``config.json`` sits on a dev box. Moving
    USERPROFILE does not move it. ``vscode_storage_base()`` reads the same
    variable, so ``discover`` would scan the real VS Code workspace storage.
    """

    def test_appdata_is_the_tmp_homes_roaming_folder(self):
        # Every OS: where APPDATA is unset, appdata_dir() falls back to
        # exactly this path, so the redirect and the fallback agree.
        assert env.appdata_dir() == Path.home() / "AppData" / "Roaming"

    @win32_only
    def test_the_appdata_bases_never_reach_the_real_appdata(self):
        for base in (env.config_base(), env.vscode_storage_base()):
            assert base.is_relative_to(Path.home()), base
            if REAL_APPDATA is not None:
                assert not base.is_relative_to(REAL_APPDATA), base

    @win32_only
    def test_find_config_without_a_flag_resolves_inside_the_tmp_home(
        self, tmp_path, monkeypatch
    ):
        # A neutral cwd: find_config tries ./magent.config.json first, and
        # this pin is about the fallback behind it.
        monkeypatch.chdir(tmp_path)
        found = paths.find_config(None)
        assert found == Path.home() / "AppData" / "Roaming" / "magent" / "config.json"
        if REAL_APPDATA is not None:
            assert not found.is_relative_to(REAL_APPDATA), found


# LOCALAPPDATA as this process inherited it, read at collection: before any
# fixture runs, so it is the value the redirect deliberately leaves alone.
_INHERITED_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")


@pytest.mark.skipif(sys.platform != "win32", reason="LOCALAPPDATA is Windows-only")
class TestLocalAppDataStaysInherited:
    """The one AppData variable the redirect must NOT move.

    ``psmux.find_psmux`` falls back to ``%LOCALAPPDATA%\\psmux\\psmux.exe``,
    where the psmux release zip installs, so a dev box whose psmux is not on
    PATH finds its real install through this variable. The tmp home's own
    ``AppData\\Local`` exists for the Win32 known-folder lookup, which never
    reads it. CI puts psmux on PATH, so no other test notices a redirect of
    LOCALAPPDATA: this is the pin that does.
    """

    def test_localappdata_is_the_inherited_one(self):
        resolved = env.localappdata_dir()
        assert resolved == Path(_INHERITED_LOCALAPPDATA)
        assert not resolved.is_relative_to(Path.home()), resolved


# What a POSIX login (or a CI runner) typically exports: the real home's own
# config dir. Moving HOME does not move an explicit XDG_CONFIG_HOME.
_REAL_LOOKING_XDG_CONFIG_HOME = REAL_HOME / ".config"


@pytest.fixture(scope="class")
def _exported_xdg_config_home():
    # Class-scoped, so it runs BEFORE conftest's function-scoped redirect: the
    # variable is genuinely ambient when the redirect fires, the way a shell
    # export is. Its own MonkeyPatch context, because the `monkeypatch` fixture
    # is function-scoped; the context restores the prior value (or its absence).
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("XDG_CONFIG_HOME", str(_REAL_LOOKING_XDG_CONFIG_HOME))
        yield


@pytest.mark.usefixtures("_exported_xdg_config_home")
class TestAnExportedXdgConfigHomeStaysInTmp:
    """The POSIX half of the forgotten-``--config`` door.

    On Linux ``env.config_base()`` is ``env.xdg_config_home()``: an exported
    ``XDG_CONFIG_HOME`` wins over ``~/.config``, so moving HOME leaves a
    forgotten ``--config`` resolving the developer's (or runner's) real
    ``$XDG_CONFIG_HOME/magent/config.json``. ``vscode_storage_base()`` reads
    the same variable. The Linux branch is forced here so the pin holds on
    every OS, including the Windows box it is developed on.
    """

    def test_xdg_config_home_is_the_tmp_homes_config_dir(self):
        resolved = env.xdg_config_home()
        assert resolved == Path.home() / ".config"
        assert not resolved.is_relative_to(_REAL_LOOKING_XDG_CONFIG_HOME)

    def test_the_linux_config_bases_resolve_inside_the_tmp_home(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        for base in (env.config_base(), env.vscode_storage_base()):
            assert base == Path.home() / ".config", base

    def test_find_config_without_a_flag_on_linux_resolves_inside_the_tmp_home(
        self, tmp_path, monkeypatch
    ):
        # A neutral cwd, as in the APPDATA pin: this is about the fallback.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "platform", "linux")
        found = paths.find_config(None)
        assert found == Path.home() / ".config" / "magent" / "config.json"
        assert not found.is_relative_to(_REAL_LOOKING_XDG_CONFIG_HOME), found


class TestToolCachesSurviveTheRedirect:
    """A redirected home moves every tool cache keyed off ``~``, not just
    magent's own state -- and a cache the CI job populated in the runner's real
    home before pytest started is then simply GONE.

    This regressed for real: PR #183's first CI run failed all four
    `browser-upload` tests with `BrowserType.launch: Executable doesn't exist
    at /tmp/pytest-of-runner/pytest-0/<test>-home/.cache/ms-playwright/...`,
    because the job's `playwright install --with-deps chromium` step writes the
    runner's home and Playwright resolves that cache at launch time.

    The browser tier is CI-only, so nothing local can catch this; these pins
    run everywhere and fail the moment the export goes away or drifts.
    """

    def test_the_browser_cache_is_pinned_outside_the_tmp_home(self):
        pinned = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
        assert pinned == PLAYWRIGHT_BROWSERS_PATH
        # The whole point: NOT under the home this test was given.
        assert not pinned.is_relative_to(Path.home())

    def test_the_pin_survives_alongside_a_still_redirected_magent_home(self):
        # It must move the browser BINARIES only. If ~/.magent came back with
        # it, the browser tier would be uploading into the developer's fleet.
        assert Path.home() != REAL_HOME
        assert not Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).is_relative_to(
            REAL_MAGENT_DIR
        )

    @pytest.mark.parametrize(
        ("platform", "tail"),
        [
            ("linux", (".cache", "ms-playwright")),
            ("darwin", ("Library", "Caches", "ms-playwright")),
            ("win32", ("AppData", "Local", "ms-playwright")),
        ],
    )
    def test_the_location_matches_playwrights_own_per_os_default(
        self, monkeypatch, platform, tail
    ):
        # The mapping is the part that can silently drift: point it one
        # directory wrong and the browser job fails with the same "Executable
        # doesn't exist" it failed with before the fix. ubuntu is the platform
        # the browser job actually runs on; the other two are pinned so a
        # future non-linux browser leg does not inherit a guess.
        monkeypatch.setattr(sys, "platform", platform)
        assert _playwright_browsers_path() == REAL_HOME.joinpath(*tail)


# Windows PowerShell, the shell the Session-0 hand-off launcher really runs.
_POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None

# The same lookup .NET's GetFolderPath makes, from a child that has no startup
# side effects of its own. KF_FLAG_DEFAULT (0) verifies the folder exists, so a
# missing one is an HRESULT rather than a path. It prints UTF-8 and the pin
# decodes UTF-8: a redirected stdout is cp1252, which cannot carry every
# profile name.
_KNOWN_FOLDER_CHILD = """
import ctypes, sys, uuid
sys.stdout.reconfigure(encoding="utf-8")
guid = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(sys.argv[1]).bytes_le)
path = ctypes.c_wchar_p()
hr = ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path))
print(path.value if hr == 0 else f"HRESULT 0x{hr & 0xFFFFFFFF:08X}")
ctypes.windll.ole32.CoTaskMemFree(path)
"""

_FOLDERID_LOCAL_APPDATA = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"
_FOLDERID_ROAMING_APPDATA = "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"


def _real_known_folder(folder_id: str) -> Path | None:
    """The same lookup, made in THIS process at import -- i.e. at collection,
    before any fixture has redirected the profile. None off Windows or when
    the folder does not resolve."""
    if sys.platform != "win32":
        return None
    guid = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    path = ctypes.c_wchar_p()
    hr = ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path))
    try:
        return Path(path.value) if hr == 0 and path.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def _redirected_off_profile(folder: Path | None, profile: Path) -> bool:
    """True only on a POSITIVE detection: the folder resolved, somewhere that
    is not under the profile. An unresolved folder is not a reason to skip."""
    return folder is not None and not folder.is_relative_to(profile)


# GPO folder redirection can move Roaming AppData (never Local) to a share.
# The registry then names that share outright, not %USERPROFILE%\..., so the
# lookup answers it under ANY USERPROFILE -- the tmp home included -- and the
# Roaming pins could only fail there. No leak is possible on such a profile
# either: the lookup resolves, just not into tmp. Only the Roaming pins
# consult this; the Local ones guard the actual cache leak and never skip.
_REAL_ROAMING_APPDATA = _real_known_folder(_FOLDERID_ROAMING_APPDATA)
roaming_in_profile = pytest.mark.skipif(
    _redirected_off_profile(_REAL_ROAMING_APPDATA, REAL_HOME),
    reason=(
        f"this profile's Roaming AppData is redirected to {_REAL_ROAMING_APPDATA}, "
        f"outside {REAL_HOME}, so the lookup answers it under any USERPROFILE"
    ),
)


class TestTheRoamingSkipIsADetection:
    """The skip above must fire on a redirected profile and nowhere else --
    a detector that is wrong in the other direction silently retires a pin."""

    def test_a_folder_under_the_profile_runs_the_pins(self, tmp_path):
        assert not _redirected_off_profile(tmp_path / "AppData" / "Roaming", tmp_path)

    def test_a_folder_off_the_profile_skips_them(self, tmp_path):
        assert _redirected_off_profile(tmp_path / "share" / "Roaming", tmp_path / "me")

    def test_an_unresolved_folder_runs_the_pins(self, tmp_path):
        assert not _redirected_off_profile(None, tmp_path)


@pytest.mark.skipif(
    sys.platform != "win32", reason="the Windows known-folder API; POSIX has none"
)
class TestTheTmpHomeResolvesItsKnownFolders:
    """A redirected USERPROFILE moves the Windows known folders with it --
    but only if the profile tree they name exists.

    Against an EMPTY tmp home, .NET's ``GetFolderPath('LocalApplicationData')``
    inside a powershell.exe child answers ``''``: it resolves the folder under
    the redirected profile, finds no ``AppData\\Local`` there, and gives up.
    PowerShell builds its ModuleAnalysisCache path from that answer, so the
    path turns CWD-relative, and a real hand-off launcher that lived ~12s past
    module analysis wrote ``Microsoft\\Windows\\PowerShell\\ModuleAnalysisCache``
    into pytest's cwd: the repo checkout, one ``git add -A`` from a commit.

    These pin the resolution itself, not the cache flush, which is
    timer-driven and was never seen under ~12s. powershell.exe creates
    ``AppData\\Roaming`` on its own at startup (measured), so its
    ApplicationData answer is right even against an empty home; the direct
    Win32 lookup has no such side effect, so it is the pin that holds each
    folder to the fixture. The Roaming cases skip only on a profile that
    redirects Roaming AppData off itself (``roaming_in_profile``).
    """

    @pytest.mark.skipif(_POWERSHELL is None, reason="powershell.exe not on PATH")
    @pytest.mark.parametrize(
        "folder",
        [
            "LocalApplicationData",
            pytest.param("ApplicationData", marks=roaming_in_profile),
        ],
    )
    def test_a_powershell_child_resolves_it_inside_the_tmp_home(self, tmp_path, folder):
        assert _POWERSHELL is not None  # narrowed by the skipif above
        r = subprocess.run(
            [
                _POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                # UTF-8 on both sides. Left alone, a redirected powershell.exe
                # writes the OEM code page and text=True decodes ANSI, so a
                # non-ASCII profile path came back mangled or not at all.
                (
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                    f"[Environment]::GetFolderPath('{folder}')"
                ),
            ],
            # Not the checkout: at red, this child must not be the one that
            # drops a cache into it.
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            # A failing child's stderr need not be UTF-8; a strict decode
            # would lose the diagnostic the assert below prints.
            errors="replace",
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        resolved = r.stdout.strip()
        assert resolved, (
            f"GetFolderPath('{folder}') is empty under the tmp home, so anything "
            "PowerShell derives from it is relative to the CWD"
        )
        assert Path(resolved).is_relative_to(Path.home()), (
            f"{folder} resolved to {resolved}, outside the tmp home {Path.home()}"
        )

    @pytest.mark.parametrize(
        ("folder", "folder_id"),
        [
            ("LocalAppData", _FOLDERID_LOCAL_APPDATA),
            pytest.param(
                "RoamingAppData", _FOLDERID_ROAMING_APPDATA, marks=roaming_in_profile
            ),
        ],
    )
    def test_the_win32_lookup_resolves_it_inside_the_tmp_home(
        self, tmp_path, folder, folder_id
    ):
        r = subprocess.run(
            [sys.executable, "-c", _KNOWN_FOLDER_CHILD, folder_id],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            # Only stdout is reconfigured: a traceback on stderr is cp1252.
            errors="replace",
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        resolved = r.stdout.strip()
        assert Path(resolved).is_relative_to(Path.home()), (
            f"SHGetKnownFolderPath({folder}) answered {resolved!r}; wanted a "
            f"path inside the tmp home {Path.home()}"
        )


class TestTheTripwireFires:
    """The per-test guards, driven rather than described."""

    @needs_tripwire
    def test_a_child_env_aimed_at_the_real_home_is_refused_before_it_spawns(self):
        # The pre-fix shape of tests/e2e/test_up.py::_run: an env built from a
        # process environment that still carried the developer's home. The
        # check runs BEFORE the child is created, so even a guard that is wrong
        # costs a test error rather than a damaged fleet.
        with pytest.raises(BaseException, match="REAL-HOME LEAK"):
            subprocess.run(
                [sys.executable, "-c", "pass"],
                check=False,
                env={
                    **os.environ,
                    "USERPROFILE": str(REAL_HOME),
                    "HOME": str(REAL_HOME),
                },
            )

    def test_a_redirected_child_env_is_let_through(self):
        home = str(Path.home())
        r = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "USERPROFILE": home, "HOME": home},
        )
        assert r.returncode == 0, r.stderr

    def test_an_import_bound_constant_pointed_back_at_the_real_home_is_named(self):
        # The regression that reopens defect #2: a new ~/.magent constant added
        # to src, bound at import against the real home, invisible to an
        # environment redirect. The scan finds it by inspection, so nobody has
        # to remember to extend a list.
        #
        # Its own MonkeyPatch context, not the `monkeypatch` fixture: the
        # tripwire shares that fixture instance and runs its check BEFORE the
        # shared teardown, so a leak planted through it would (correctly) fail
        # this test's teardown instead of being asserted on here.
        assert _leaked_module_paths() == []
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(
                "magent.cli.attach._LAST_HOST_FILE",
                REAL_MAGENT_DIR / "last-attach-host",
            )
            leaks = _leaked_module_paths()
        assert any("_LAST_HOST_FILE" in leak for leak in leaks), (
            "a constant pointing at the real ~/.magent went unreported"
        )
        assert _leaked_module_paths() == []


class TestEnvInspection:
    @pytest.mark.parametrize("key", ["USERPROFILE", "HOME"])
    def test_the_offending_key_is_named(self, key):
        assert _env_points_at_real_home({key: str(REAL_HOME)}) == key

    def test_a_tmp_home_is_clean(self, tmp_path):
        assert _env_points_at_real_home({"HOME": str(tmp_path)}) is None

    def test_an_inherited_env_is_clean(self):
        # env=None means "inherit os.environ", which the redirect already owns.
        assert _env_points_at_real_home(None) is None
