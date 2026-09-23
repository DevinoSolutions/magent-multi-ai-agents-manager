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

import os
import subprocess
import sys
from pathlib import Path

import pytest

from magent import lockfile
from tests.conftest import (
    PLAYWRIGHT_BROWSERS_PATH,
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
