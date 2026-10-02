import contextlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner

from magent import agent_state, env, log, node_sync, procs, reap
from magent.grid import MonitorRect
from magent.platform import (
    HandoffResult,
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    VSCodeLaunchOpts,
)
from magent.titles import get_leaf_name
from tests.unit._fake_ssh import SteppedClock, make_fake_ssh

# --- Real-home isolation ------------------------------------------------------
# Captured at conftest IMPORT time, i.e. before any fixture has had the chance
# to redirect the environment: this is the one place that still knows where the
# developer's actual home is, and both the redirect (which must not point at it)
# and the tripwire (which watches it) need that answer.
REAL_HOME = Path.home()
REAL_MAGENT_DIR = REAL_HOME / ".magent"
# The product state directories under that home -- NOT the home itself, because
# on Windows the pytest tmp root lives at %LOCALAPPDATA%\Temp, i.e. inside it.
# These are the trees a leaking test actually damages.
_REAL_STATE_ROOTS = (REAL_MAGENT_DIR, REAL_HOME / ".claude")
# The inherited APPDATA, captured here for the same reason. On Windows it IS
# env.config_base(), so the developer's real config sits at
# REAL_APPDATA\magent\config.json. None where it is unset (off Windows).
REAL_APPDATA = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else None

# Tests under this directory keep the machine's own home. tests/platform is the
# CI-only tier that drives REAL windows, monitors and psmux against the session
# it runs in; a redirected home would break it and it never runs on a dev box.
_KEEP_REAL_HOME_DIRS = ("platform",)

# The HOME family. On Windows `Path.home()` reads USERPROFILE first and falls
# back to HOMEDRIVE+HOMEPATH, so redirecting HOME alone silently does nothing
# there -- the exact hole that let `magent down` act on the real ~/.magent.
_HOME_VARS = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


def _playwright_browsers_path() -> Path:
    """Playwright's own default browser cache, under the ORIGINAL home.

    Playwright resolves this relative to ~ at LAUNCH time, so a redirected home
    sends it looking in a tmp directory the `playwright install` step never
    wrote to. Mirrors Playwright's documented per-OS default exactly; the
    mapping is pinned by tests/unit/test_home_isolation.py.
    """
    if sys.platform == "win32":
        return REAL_HOME / "AppData" / "Local" / "ms-playwright"
    if sys.platform == "darwin":
        return REAL_HOME / "Library" / "Caches" / "ms-playwright"
    return REAL_HOME / ".cache" / "ms-playwright"


PLAYWRIGHT_BROWSERS_PATH = _playwright_browsers_path()

# ~/.magent paths that product modules bind at IMPORT time. The env redirect
# below cannot reach these: the constant was computed the moment the module was
# first imported (during collection), against the real home. Each is a file a
# test could otherwise write, overwrite or UNLINK on a live machine --
# `last-attach-host` is what a real `magent down --all` forwards the shutdown
# to over ssh, and `hotkey-supervisor.lock`'s owner is a live `magent serve`.
_IMPORT_BOUND_PATHS = (
    ("magent.cli.attach", "_LAST_HOST_FILE", "last-attach-host"),
    ("magent.cli.attention_cmd", "_PID_PATH", "attention.pid"),
    ("magent.node_sync", "_PID_PATH", f"{node_sync.HEARTBEAT_NAME}.pid"),
    ("magent.node_sync", "_FOLLOWS_PATH", f"{node_sync.HEARTBEAT_NAME}.config"),
    ("magent.cli.session_picker", "_FOCUS_TARGET_FILE", "focus-target"),
    ("magent.cli.session_picker", "_PICKER_ATTACHED_FILE", "picker-attached"),
    ("magent.upload_server", "_FOCUS_TARGET_FILE", "focus-target"),
    ("magent.upload_server", "_PICKER_ATTACHED_FILE", "picker-attached"),
    ("magent.upload_server", "_UPLOAD_DIR", "uploads"),
    ("magent.psmux", "DECOR_STAMP", "decor.stamp"),
    ("magent.nodes", "NODES_DIR", "nodes"),
    ("magent.nodes", "NODE_MAP_PATH", "nodes/node-map.json"),
    # win32-only module (it raises ImportError elsewhere by design), so this
    # entry is skipped rather than imported off-Windows.
    ("magent.hotkey", "_PID_PATH", "hotkey.pid"),
    ("magent.hotkey", "_MANIFEST_PATH", "hotkey.json"),
)


def _keeps_real_home(request) -> bool:
    """True when this test deliberately runs against the machine's own home."""
    if request.node.get_closest_marker("real_home") is not None:
        return True
    return any(
        part in _KEEP_REAL_HOME_DIRS for part in Path(str(request.node.path)).parts
    )


@pytest.fixture(autouse=True)
def _isolate_magent_home(request, tmp_path, monkeypatch):
    """Every test's ~/.magent reads and writes land under tmp_path, never the
    real one -- autouse so no test can forget it (and so a developer machine's
    live agent-state records or ~/.magent/.env can't leak into assertions).

    Three layers, because ``~/.magent`` is reached three different ways:

    1. the module-level path constants magent binds at import (LOG_DIR &c),
    2. the HOME family in the process environment, which covers every
       call-time ``Path.home()`` (``lockfile.exclusive_lock`` is one) AND
       every child process, since they inherit ``os.environ``. With it go
       ``APPDATA`` (to ``<home>/AppData/Roaming``) and ``XDG_CONFIG_HOME``
       (to ``<home>/.config``), the variables ``env.config_base()`` reads
       instead of ~, so a test that forgets ``--config`` cannot find the
       real config. The empty ``<home>/AppData/Local`` and
       ``<home>/AppData/Roaming`` folders are created for the Windows
       known-folder lookups, which need them to exist. ``LOCALAPPDATA``
       stays inherited on purpose (``find_psmux``'s fallback),
    3. the import-bound ``~/.magent`` constants layer 2 is too late for.

    Layer 2 is the one that was missing, and its absence is not theoretical: a
    local `pytest tests/e2e/` run had `magent down --all` stop this machine's
    real Alt+V listener and then try to forward the shutdown over ssh to the
    host recorded in the real ``~/.magent/last-attach-host``.
    """
    monkeypatch.setattr(log, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(log, "HEARTBEAT_DIR", tmp_path / "hb")
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "agent-state")
    monkeypatch.setattr(env, "ENV_FILE", tmp_path / "env-file")
    monkeypatch.setattr(env, "_cached_env", None)
    if not _keeps_real_home(request):
        # A SIBLING of tmp_path, not a child: tmp_path is a working directory
        # tests scan, glob and assert the contents of (scan_for_projects walks
        # it looking for repos), and a stray `home/` inside it changes those
        # answers. Same lifetime, no collisions with the fixture's own tree.
        home = tmp_path.parent / f"{tmp_path.name}-home"
        home.mkdir(exist_ok=True)
        # The profile folders the Windows known-folder lookups name. The
        # USERPROFILE redirect below moves those folders into this home, but
        # the lookup also checks that the folder EXISTS: against an empty home
        # .NET's GetFolderPath('LocalApplicationData') answers '' in a
        # powershell.exe child, its ModuleAnalysisCache path turns
        # CWD-relative, and a long-lived hand-off launcher wrote
        # Microsoft\Windows\PowerShell\ModuleAnalysisCache into the repo
        # checkout. The lookup does not read LOCALAPPDATA/APPDATA; creating
        # the folders is the fix. Empty dirs; harmless off Windows.
        (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
        (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
        # APPDATA is a door of its own: on Windows it IS env.config_base(),
        # so a test that forgot --config resolved the developer's real
        # %APPDATA%\magent\config.json (and discover scanned the real VS Code
        # storage). Point it at this home's Roaming folder -- the path
        # appdata_dir() falls back to where APPDATA is unset, so every OS
        # gets the same answer. LOCALAPPDATA stays inherited: find_psmux's
        # %LOCALAPPDATA%\psmux fallback must still find the real install, and
        # wt_keys resolves through its own seam.
        monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        # The same door on Linux: config_base() is xdg_config_home(), and an
        # exported XDG_CONFIG_HOME (a login's, a runner's) wins over
        # ~/.config, so moving HOME alone left it pointing at the real one.
        # <home>/.config is what xdg_config_home() falls back to when the
        # variable is unset, so a box that never exported it sees no change.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        drive, tail = os.path.splitdrive(str(home))
        values = (str(home), str(home), drive, tail or os.sep)
        for var, value in zip(_HOME_VARS, values, strict=True):
            monkeypatch.setenv(var, value)
        assert Path.home() == home, (
            f"HOME redirect did not take: Path.home() is {Path.home()}, wanted {home}"
        )
        for module, attr, leaf in _IMPORT_BOUND_PATHS:
            # ImportError: magent.hotkey off-Windows, where the constant does
            # not exist because nothing can reach it either.
            with contextlib.suppress(ImportError):
                monkeypatch.setattr(f"{module}.{attr}", home / ".magent" / leaf)
        # A redirected home moves more than magent's own state: it also moves
        # every tool cache keyed off ~, and the browser tier's CI job installs
        # Chromium into the RUNNER's home with `playwright install` in a step
        # that runs long before pytest. Playwright resolves that cache at
        # launch time, so the redirect above sent it hunting inside this test's
        # tmp home ("Executable doesn't exist at .../-home/.cache/ms-playwright
        # /chromium_headless_shell-.../..."), failing all four browser tests.
        # Pin the cache to where the install actually put it. This moves the
        # browser BINARIES only -- ~/.magent stays redirected, so the browser
        # tier is still fully isolated from the developer's fleet -- and an
        # explicit PLAYWRIGHT_BROWSERS_PATH already in the environment wins.
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            monkeypatch.setenv(
                "PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS_PATH)
            )
    # Isolating ENV_FILE (above) is not enough: an exported process-env
    # MAGENT_* var (a dev shell's real MAGENT_SENTRY_DSN) is still read by
    # get_env(), and on 2026-07-07 that leaked fake test errors to prod Sentry
    # (MAGENT-1..4). Strip every MAGENT_* key so no test can init real
    # Sentry or see ambient config -- unknown keys swept too, matching env.py's
    # own closed-schema scan.
    for _key in list(os.environ):
        if _key.upper().startswith("MAGENT_"):
            monkeypatch.delenv(_key, raising=False)
    # ...with one deliberate exception, put back after the sweep: the attention
    # daemon supervises `magent serve`, and its loop IS driven for real in unit
    # tests (`attention --ticks N`). Today every such test uses the default
    # `uploadServer: false`, so the watchdog is never built -- but a future test
    # that flips that key would otherwise spawn a REAL detached upload server on
    # the machine running the suite, on the config's port, with no pid for any
    # teardown to kill. Tests that are ABOUT the supervisor set it back to "1".
    monkeypatch.setenv("MAGENT_UPLOAD_SUPERVISOR", "0")
    # ...and its mirror image: `magent serve` supervises the attention daemon.
    # Any test that runs a real serve (unit `run_server`, every e2e/dist/soak
    # serve fixture) would otherwise start a REAL detached `attention -d` behind
    # it the moment the redirected home held an attention heartbeat -- a daemon
    # that badges and flashes the developer's own windows and that no teardown
    # knows the pid of. Tests that are ABOUT that supervisor set it back to "1".
    monkeypatch.setenv("MAGENT_ATTENTION_SUPERVISOR", "0")
    # ...and a second, for the sharpest reason of the three. The psmux priority
    # sweep (psmux.boost_priority) is the ONE thing in this product that reaches
    # processes it did not spawn, by IMAGE NAME, and no HOME redirect can
    # contain it: a test that starts a real `serve` or `attention -d` on this
    # developer's box would re-prioritise the machine's entire live fleet (169
    # psmux.exe at last count). Off for every tier; the tests that are ABOUT the
    # sweep drive the seam directly and never the real Windows primitives.
    monkeypatch.setenv("MAGENT_PSMUX_BOOST", "0")
    # ...and a third, whose reason is the RUNNER rather than the developer's
    # fleet: the Session-0 hand-off asks "am I in an interactive logon session?"
    # and a CI agent (or a test driven over ssh) legitimately is not. With the
    # default `handoff` policy such a runner would create REAL scheduled tasks
    # aimed at somebody's desktop; with `refuse` every bring-up test would fail
    # for a reason that has nothing to do with the code under test. `allow`
    # means "run it right here", i.e. exactly today's behaviour, on every
    # machine the suite runs on. Tests that are ABOUT the hand-off set the
    # policy explicitly.
    monkeypatch.setenv("MAGENT_SESSION0_POLICY", "allow")
    # ...and a fourth, with the longest reach of all: `magent serve` keeps the
    # node sync daemon alive, and that daemon ssh-es into every machine in
    # settings.nodes with the developer's own keys, every pull interval. A test
    # that starts a real serve would dial real machines. Off for every tier; the
    # tests that are ABOUT the supervisor set it back to "1".
    monkeypatch.setenv("MAGENT_NODE_SYNC", "0")
    # ...and a fifth, the SHARPEST: the idle reaper (reap.sweep_once, owned by
    # `magent serve`) is the only thing in the product that TERMINATES processes
    # it did not spawn, by psmux session name and ~/.claude session file -- and
    # no HOME redirect contains either. A test that starts a real serve or
    # attention -d on a developer's box with reaping on would hard-kill that
    # box's live fleet. Off for every tier; reaper tests turn it on IN PROCESS
    # only (monkeypatch.setenv + resetting env._cached_env).
    monkeypatch.setenv("MAGENT_IDLE_REAP", "0")
    # ...and a sixth: the tab-icon fragment is written into the REAL
    # %LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments folder, which no HOME
    # redirect contains (LOCALAPPDATA is inherited), and Windows Terminal
    # picks it up live. In-process the `_no_real_wt_fragment` fixture below
    # re-aims the resolver; this pin covers every CHILD process a test starts.
    # Tests that are ABOUT the feature turn it on in process.
    monkeypatch.setenv("MAGENT_WT_ICONS", "0")
    log.reset_logging()
    yield
    log.reset_logging()


@pytest.fixture(autouse=True)
def _no_inherited_git_repo_env(monkeypatch):
    """No test inherits a variable that AIMS git at a repo (every name in
    ``env.GIT_LOCAL_ENV_VARS``, i.e. ``git rev-parse --local-env-vars``).

    Same family as the HOME redirect, and not theoretical: the husky pre-push
    hook runs the full gate, pytest included, with the hook's GIT_DIR exported
    -- an ABSOLUTE path when pushing from a worktree, which is how this
    project works. Under it a fixture's ``git init --bare`` rewrote the real
    repo's shared config to ``core.bare=true``, its commit landed on the real
    checked-out branch, and its ``push -u origin main`` went to the real
    origin (reproduced against a scratch victim). Deleting them here covers
    every fixture's git child and every product git read under test at once.
    """
    for name in env.GIT_LOCAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_ssh(monkeypatch):
    """No test resolves the REAL ``ssh`` client. A test that installed no fake
    sees "not installed" (``remote_mux.run`` raises RemoteError rc 127), a
    shape every caller already handles.

    Same family as ``MAGENT_PSMUX_BOOST=0``, for its sharpest reason: a node
    call dials a real machine on the network under the developer's own keys,
    and no HOME redirect contains a binary on PATH. Patched on the MODULE
    attribute, so ``test_remote_mux.py``'s by-value import of ``find_ssh`` (the
    test that proves PATH resolution) still gets the real resolver; the
    ``fake_ssh`` fixture patches the same attribute afterwards and wins. The
    ``needs_ssh`` node tier re-points it at the real client deliberately.

    The attach pane's resolver is guarded too, at both of its halves:
    ``attach_client.find_ssh`` (so an in-process pane argv names bare ``ssh``)
    and ``attach_client._system_directory`` (so no test reads the real
    Windows OpenSSH, including through the by-value real resolvers).
    """
    monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: None)
    monkeypatch.setattr("magent.attach_client.find_ssh", lambda: None)
    monkeypatch.setattr("magent.attach_client._system_directory", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    """No test resolves the REAL ``gh``: it holds the developer's GitHub token,
    and ``node setup`` registers an ssh key to their account with it. Same
    device as ``_no_real_ssh``; the ``fake_gh`` fixture wins over it."""
    monkeypatch.setattr("magent.remote_mux.find_gh", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    """No test resolves the REAL ``claude`` for ``node_auth``: ``claude
    setup-token`` mints a year-long credential on the developer's Claude
    subscription and opens their browser to do it. Same device as
    ``_no_real_gh``; the ``fake_claude`` fixture wins over it."""
    monkeypatch.setattr("magent.node_auth.find_claude", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_wt_fragment(tmp_path_factory, monkeypatch):
    """No test writes into (or reads from) the REAL Windows Terminal fragments
    folder. ``wt_profiles.fragments_root`` is re-aimed at a tmp dir for every
    test, so a spawn path or a ``sync`` that a test reaches by accident lands
    in tmp. Same device as ``_no_real_ssh``: patched on the MODULE attribute,
    so ``test_home_isolation`` can still import the real resolver by value.
    """
    # A unique PATH, not a directory: `mktemp` numbers its dirs by scanning every
    # sibling, so one per test (all ~8000 of them, autouse) made the suite
    # quadratic and pushed the CI quality job past its 10-minute timeout. The
    # folder comes into being only for the few tests that write a fragment.
    root = tmp_path_factory.getbasetemp() / "wt-fragments" / uuid4().hex
    monkeypatch.setattr("magent.wt_profiles.fragments_root", lambda: root)
    # A lock found held by one test must not mute the next one's syncs.
    monkeypatch.setattr("magent.wt_profiles._launch.lock_lost", False)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A fake ``claude`` (the one fake, tests/unit/_fake_ssh.py, under another
    name) wired in as node_auth's local claude."""
    fake = make_fake_ssh(tmp_path, name="claude")
    monkeypatch.setattr("magent.node_auth.find_claude", lambda: fake.path)
    return fake


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """A real on-disk fake ``ssh`` wired in as remote_mux's client (THE fake:
    tests/unit/_fake_ssh.py)."""
    fake = make_fake_ssh(tmp_path)
    monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: fake.path)
    return fake


# Every fake gh call is a cold cmd.exe (Windows) + interpreter start, ~130ms
# on an idle box; under a loaded desktop one took past the product's 20s bound
# and a pin that never meant to test a timeout read the timeout row. A test
# that does mean it sets its own short GH_TIMEOUT_S, which wins over this.
FAKE_GH_TIMEOUT_S = 120.0


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A fake ``gh`` (the one fake, tests/unit/_fake_ssh.py, under another
    name) wired in as remote_mux's local gh, with headroom for a slow spawn
    (``FAKE_GH_TIMEOUT_S``)."""
    fake = make_fake_ssh(tmp_path, name="gh")
    monkeypatch.setattr("magent.remote_mux.find_gh", lambda: fake.path)
    monkeypatch.setattr("magent.remote_mux.GH_TIMEOUT_S", FAKE_GH_TIMEOUT_S)
    return fake


@pytest.fixture
def stepped_clock(monkeypatch):
    """remote_mux's clock replaced by a ``SteppedClock`` (tests/unit/
    _fake_ssh.py): a flood pin's first words come before the cap on any
    platform's clock. remote_mux reads ``time.monotonic`` alone, so the
    stand-in carries only that -- any other ``time`` use there would fail
    loudly, never read the real clock.

    Flood pins only: ``_finish``'s deadline reads this same clock, so a call
    that does NOT pass the cap finds its whole bound spent at the next read
    (``deadline - 1000.0`` is below zero) and stops waiting on its drains
    and the child at once -- a false timeout, not the reply."""
    clock = SteppedClock()
    monkeypatch.setattr(
        "magent.remote_mux.time", types.SimpleNamespace(monotonic=clock.monotonic)
    )
    return clock


# --- The tripwire -------------------------------------------------------------
# The redirect above is the fix; this is the alarm that keeps it fixed, because
# the failure mode is silent BY CONSTRUCTION: a test that forgets the redirect
# still passes -- it just stops the machine's real Alt+V listener, deletes a
# real lock file, or (the observed case) hangs 124s forwarding `down` over ssh
# to a host nobody meant to contact.
#
# The obvious guard -- diff the real ~/.magent before and after each test -- was
# built first and REJECTED as flaky: a dev box runs a live magent fleet, and on
# the very first run of this suite the fleet respawned its own listener and
# rewrote hotkey.pid/hotkey.json mid-test. Nothing about that mutation is
# attributable to the test that happened to be running. So the per-test guards
# below are DETERMINISTIC ones that watch the two doors instead of the room:
#
#   A. redirect integrity -- ~ still resolves into tmp, and no magent module
#      holds an import-bound Path under the real home (the shape of defect #2:
#      lockfile / `_LAST_HOST_FILE`-style constants the env redirect is too
#      late for, INCLUDING ones added to src later, which is the regression
#      this catches without anyone remembering to update a list);
#   B. child-env integrity -- no subprocess is spawned with an explicit env
#      that points HOME/USERPROFILE at the real home (the shape of defect #1).
#
# Neither reads the real ~/.magent at all, so neither can flake on fleet churn.
# The filesystem diff survives only as C: session-scoped and ADVISORY (a
# terminal-summary note, never a failure), which is the honest severity for an
# observation that cannot name a culprit.

# Magent module attributes allowed to sit under the real home even in a
# redirected test: none. Kept as a named set so an exception, if one is ever
# genuinely needed, has to arrive with a name rather than weaken the check.
_HOME_PATH_ALLOWED: frozenset[str] = frozenset()


def _leaked_module_paths() -> list[str]:
    """Every ``magent.*`` module attribute that is a Path under the real home.

    Import-bound constants are invisible to an environment redirect: the value
    was computed the first time the module was imported, during collection,
    against the machine's own home. This finds them by inspection rather than
    by memory, so a NEW one added to src is caught by the next test run instead
    of by a developer noticing their fleet died.
    """
    leaks = []
    for name, module in list(sys.modules.items()):
        if not name.startswith("magent") or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") or not isinstance(value, Path):
                continue
            qualified = f"{name}.{attr}"
            if qualified in _HOME_PATH_ALLOWED:
                continue
            try:
                if any(value.is_relative_to(root) for root in _REAL_STATE_ROOTS):
                    leaks.append(f"{qualified} = {value}")
            except (OSError, ValueError):  # pragma: no cover - defensive
                continue
    return sorted(leaks)


def _env_points_at_real_home(env) -> str | None:
    """The offending key, if this child env would hand the child the real home."""
    if not env:
        return None
    for key in ("USERPROFILE", "HOME"):
        raw = env.get(key)
        if not raw:
            continue
        try:
            if Path(raw) == REAL_HOME:
                return key
        except (OSError, ValueError):  # pragma: no cover - defensive
            continue
    return None


def _tripwire_disabled() -> bool:
    # CI homes are disposable and several tiers legitimately write them (the
    # ssh flagship seeds the runner's own ~/), so this is a dev-machine guard.
    # MDTEST_REAL_HOME_ALLOWED is the manual escape hatch.
    return bool(
        os.environ.get("CI")
        or os.environ.get("GITHUB_ACTIONS")
        or os.environ.get("MDTEST_REAL_HOME_ALLOWED")
    )


_LEAK_HELP = (
    "\nThis is the defect class the real-home isolation work exists to stop: a "
    "test acting on the developer's live fleet instead of on a fixture. Set "
    "MDTEST_REAL_HOME_ALLOWED=1 to silence the guard, or mark the test "
    "@pytest.mark.real_home if it genuinely owns the real home."
)


@pytest.fixture(autouse=True)
def _real_home_tripwire(request, monkeypatch, _isolate_magent_home):
    """Guards A and B: fail LOUDLY the moment the redirect stops holding."""
    if _tripwire_disabled() or _keeps_real_home(request):
        yield
        return

    # B: every child process, checked at the spawn. `subprocess.run`,
    # `check_output` and `Popen` all funnel through the module attribute, so
    # one wrapper covers the lot -- including a helper that hand-builds `env=`
    # and drops the HOME family, which is exactly how test_up.py's `_run`
    # reached the real ~/.magent.
    real_popen = subprocess.Popen

    def _guarded_popen(*args, **kwargs):
        offender = _env_points_at_real_home(kwargs.get("env"))
        if offender is not None:
            pytest.fail(
                f"REAL-HOME LEAK: a child process was spawned with {offender}="
                f"{REAL_HOME}, so it would read and write the developer's "
                f"actual {REAL_MAGENT_DIR}.\nBuild the child env with a tmp "
                "home and set the WHOLE family -- HOME, USERPROFILE, "
                "HOMEDRIVE, HOMEPATH -- because Path.home() reads USERPROFILE "
                "on Windows and HOME alone changes nothing there." + _LEAK_HELP
            )
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _guarded_popen)

    yield

    # A: the redirect still holds, and nothing reached ~ around it.
    assert Path.home() != REAL_HOME, (
        f"REAL-HOME LEAK: ~ resolves to {REAL_HOME} again at the end of this "
        "test -- something restored the real HOME family mid-test." + _LEAK_HELP
    )
    leaks = _leaked_module_paths()
    assert not leaks, (
        "REAL-HOME LEAK: magent module state points into the developer's real "
        "home, where an environment redirect cannot reach it because the value "
        "was bound at import:\n  "
        + "\n  ".join(leaks)
        + "\nAdd it to _IMPORT_BOUND_PATHS in tests/conftest.py so every test "
        "gets a redirected copy." + _LEAK_HELP
    )


# --- C: the advisory filesystem observation -----------------------------------
_TRIPWIRE_WATCH = ("hotkey.pid", "hotkey.json", "last-attach-host", ".env")
_TRIPWIRE_GLOBS = ("upload_server-*.pid",)
_OBSERVED_KEY = pytest.StashKey[list]()


def _real_home_fingerprint() -> dict[str, object]:
    """Contents (not mtimes -- an identical rewrite changed nothing) of the real
    ~/.magent files a healthy fleet leaves alone. Deliberately excludes
    hotkey.heartbeat and logs/ state/ uploads/, which it rewrites constantly."""
    fp: dict[str, object] = {".magent/": REAL_MAGENT_DIR.is_dir()}
    paths = [REAL_MAGENT_DIR / n for n in _TRIPWIRE_WATCH]
    try:
        for pattern in _TRIPWIRE_GLOBS:
            paths.extend(sorted(REAL_MAGENT_DIR.glob(pattern)))
    except OSError:  # pragma: no cover - defensive
        pass
    for path in paths:
        try:
            fp[path.name] = path.read_bytes()
        except OSError:
            fp[path.name] = None  # absent or unreadable -- both are "not there"
    return fp


@pytest.fixture(scope="session", autouse=True)
def _real_home_session_observer(request):
    """Report -- never fail -- when the real ~/.magent changed across the run.

    Session-scoped and advisory on purpose. It cannot attribute a change to a
    test (the fleet churns on its own), so failing on it would BE the flake the
    per-test guards above were designed to avoid. As a run-level note it still
    earns its place: after a suspicious run it answers "did the suite touch my
    fleet?" without anyone reconstructing it from mtimes.
    """
    if _tripwire_disabled():
        yield
        return
    before = _real_home_fingerprint()
    yield
    after = _real_home_fingerprint()
    changed = sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
    )
    if changed:
        request.config.stash[_OBSERVED_KEY] = changed


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    changed = config.stash.get(_OBSERVED_KEY, None)
    if not changed:
        return
    terminalreporter.write_sep("!", "real ~/.magent changed during this run")
    terminalreporter.write_line(
        f"{REAL_MAGENT_DIR}: {', '.join(changed)}\n"
        "ADVISORY, not a failure: a live magent fleet rewrites these on its "
        "own (a listener respawn, a `magent attach`), so this cannot name a "
        "culprit. When the cause IS a test, the per-test guards in "
        "tests/conftest.py name it."
    )


# --- The kill guards ----------------------------------------------------------
# The idle reaper's stop (reap._stop) is the one path in the product that
# TERMINATES processes, and its default snapshot is the machine's whole live
# process table -- where a stale parent pid can list a stranger under a test's
# "agent". Two guards, autouse for every tier so no test can forget them and
# no module can be outside them:
#
#   1. no live table: _stop's default snapshot is one that FAILS the test.
#      Every test hands _stop its own (a fake, or the live table cut down to
#      the pids it spawned). `@pytest.mark.live_process_table` opts out, for a
#      tier that must walk the real table; its kills still go through 2.
#   2. own processes only: every in-process kill goes through
#      procs.terminate_verified, which here fails the test unless it names a
#      process the test registered in `own_pids` -- by IDENTITY (pid, image,
#      creation time), because a registered pid that died and was reused names
#      a stranger -- before the kill reaches the OS. Under it, the kernel32
#      procs hands out lets TerminateProcess land only inside that guarded
#      call, on the pid it approved, and never lets TerminateJobObject land.
#      No opt-out: a test that has to kill registers what it spawned.
#      (os.kill, taskkill and psmux kill-server cannot be wrapped for every
#      tier -- e2e teardown uses them on its own daemons -- so reap's reach is
#      pinned instead: tests/unit/test_kill_guards.py.)


def refuse_live_snapshot(stop, monkeypatch) -> None:
    """Point ``stop``'s ``snapshot`` keyword default at a refusal. Asserts the
    keyword is there first: ``setitem`` ADDS a missing key, so after a rename
    this would guard a default nobody reads, and pass."""
    defaults = stop.__kwdefaults__ or {}
    assert "snapshot" in defaults, (
        f"{stop.__qualname__} has no `snapshot` keyword default any more; the "
        "live-table guard in tests/conftest.py must follow the rename."
    )

    def _refuse() -> None:
        pytest.fail("reap._stop was handed the live process table")

    monkeypatch.setitem(stop.__kwdefaults__, "snapshot", _refuse)


@pytest.fixture(autouse=True)
def _no_live_process_table(request, monkeypatch):
    if request.node.get_closest_marker("live_process_table") is not None:
        return
    refuse_live_snapshot(reap._stop, monkeypatch)


class OwnPids:
    """The processes a test lets die -- each one it spawned, known by its
    identity -- and the kill every in-process ``procs.terminate_verified`` goes
    through: a kill aimed at anything else fails the test before it reaches the
    OS. Keyed by identity, not pid: once a registered process dies its pid is
    free, and the real terminate_verified would verify a stranger reusing it as
    itself. ``kill`` is the real primitive unless a test swaps in a fake behind
    the guard; ``kernel32`` is what procs hands out in place of its own."""

    def __init__(self, kill, kernel32=None) -> None:
        self.owned: dict[int, procs.ProcessIdentity] = {}
        self.kill = kill
        self.real_kernel32 = kernel32
        self.approved: int | None = None  # the pid a guarded kill is ending

    def add(self, pid: int, identity: procs.ProcessIdentity) -> None:
        self.owned[pid] = identity

    def terminate_verified(self, pid, expected):
        owned = self.owned.get(pid)
        if owned is None:
            pytest.fail(f"a kill reached pid {pid}, which this test did not spawn")
        if owned != expected:
            pytest.fail(
                f"a kill reached pid {pid} as {expected}, but the process this "
                f"test spawned there is {owned}: that pid now names a process "
                "this test did not spawn"
            )
        self.approved = pid
        try:
            return self.kill(pid, expected)
        finally:
            self.approved = None

    def kernel32(self):
        return GuardedKernel32(self.real_kernel32(), self)


class GuardedKernel32:
    """kernel32 as procs hands it out under every test. Every call passes
    through except the two that end a process: TerminateProcess lands only
    inside the guarded ``terminate_verified``, on a handle this object opened
    for the very pid that call approved; TerminateJobObject never lands."""

    def __init__(self, real, guard: OwnPids) -> None:
        self._real = real
        self._guard = guard
        self._pid_of: dict[int, int] = {}  # open handle -> the pid it names

    def __getattr__(self, name):
        return getattr(self._real, name)

    def OpenProcess(self, rights, inherit, pid):
        handle = self._real.OpenProcess(rights, inherit, pid)
        if handle:
            self._pid_of[handle] = pid
        return handle

    def CloseHandle(self, handle):
        self._pid_of.pop(handle, None)
        return self._real.CloseHandle(handle)

    def TerminateProcess(self, handle, code):
        pid = self._pid_of.get(handle)
        if pid is None or pid != self._guard.approved:
            pytest.fail(
                f"TerminateProcess reached pid {pid} outside the guarded kill "
                f"(approved: {self._guard.approved}): a kill path that bypasses "
                "procs.terminate_verified"
            )
        return self._real.TerminateProcess(handle, code)

    def TerminateJobObject(self, job, code):
        pytest.fail("TerminateJobObject reached kernel32: no test kills through a job")


@pytest.fixture(autouse=True)
def own_pids(monkeypatch) -> OwnPids:
    guard = OwnPids(procs.terminate_verified, procs._kernel32)
    monkeypatch.setattr(procs, "terminate_verified", guard.terminate_verified)
    monkeypatch.setattr(procs, "_kernel32", guard.kernel32)
    return guard


@pytest.fixture
def tmp_config(tmp_path):
    """Write a config dict to a temp JSON file and return the path."""

    def _write(config_dict):
        p = tmp_path / "magent.config.json"
        p.write_text(json.dumps(config_dict))
        return str(p)

    return _write


@pytest.fixture
def fake_claude_sessions(tmp_path):
    """Create fake Claude session .jsonl files with controlled mtimes."""

    def _create(encoded_path, sessions):
        sess_dir = tmp_path / ".claude" / "projects" / encoded_path
        sess_dir.mkdir(parents=True, exist_ok=True)
        for uuid, mtime in sessions:
            f = sess_dir / f"{uuid}.jsonl"
            f.write_text('{"type":"message"}\n')
            os.utime(f, (mtime, mtime))
        return sess_dir

    return _create


@pytest.fixture
def fake_codex_sessions(tmp_path):
    """Create fake Codex session .jsonl files with CWD metadata."""

    def _create(sessions):
        sess_root = tmp_path / ".codex" / "sessions"
        for i, (cwd, uuid, mtime) in enumerate(sessions):
            day_dir = sess_root / "2026" / "06" / str(20 + i)
            day_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "timestamp": "2026-06-20T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": uuid, "cwd": cwd},
            }
            f = day_dir / f"session-{i}-{uuid}.jsonl"
            f.write_text(json.dumps(meta) + "\n")
            os.utime(f, (mtime, mtime))
        return sess_root

    return _create


class FakePlatform(Platform):
    """Test double for Platform -- records calls instead of touching real
    windows/monitors/psmux. Reused by E5 to unit-test the decomposed
    run_magent pieces (see tests/unit/test_platform_contract.py)."""

    def __init__(
        self,
        monitors=None,
        windows=None,
        supports_psmux: bool = False,
        supports_attention: bool = False,
        supports_hotkey: bool = False,
        supports_wt_keybindings: bool = False,
        supports_wt_profiles: bool = False,
        supports_attach_windows: bool = False,
        supports_nudge: bool = False,
        nudge_error: Exception | None = None,
        supports_close: bool = False,
        supports_scan: bool = False,
        cmdlines=None,
        scan_error: Exception | None = None,
        close_error: Exception | None = None,
        psmux_launch_failures=None,
        interactive_session: bool = True,
        supports_handoff: bool = False,
        handoff_result=None,
        pane_reset: str | None = None,
    ):
        self._monitors = (
            monitors
            if monitors is not None
            else [
                MonitorRect(x=0, y=0, w=1920, h=1080, is_primary=True, scale_factor=1.0)
            ]
        )
        self._windows = windows if windows is not None else {}
        self._supports_psmux = supports_psmux
        self._supports_attention = supports_attention
        self._supports_hotkey = supports_hotkey
        self._supports_wt_keybindings = supports_wt_keybindings
        self._supports_wt_profiles = supports_wt_profiles
        self._supports_attach_windows = supports_attach_windows
        self._supports_nudge = supports_nudge
        self._nudge_error = nudge_error
        self._supports_close = supports_close
        self._supports_scan = supports_scan
        self._cmdlines = list(cmdlines or [])
        self._scan_error = scan_error
        self._close_error = close_error
        self._next_handle = 1
        self.closed: list = []
        self.scanned: list[list[str]] = []
        self.dpi_aware_calls = 0
        self.launched_terminals: list[TerminalLaunchOpts] = []
        self.launched_vscode: list[VSCodeLaunchOpts] = []
        self.launched_psmux: list[PsmuxWindowOpts] = []
        # A session name in `psmux_launch_failures` does NOT become live on its
        # first launch -- the storm-wedged psmux server the bring-up creation
        # verify exists to catch (new-session exits 0, no server answers). A
        # later launch of the same name succeeds, so a respawn is provable.
        self._psmux_launch_failures = set(psmux_launch_failures or ())
        # Session-0 hand-off. The defaults are the ordinary desktop: an
        # interactive logon session and no hand-off mechanism, which is what
        # every platform except Windows reports and what keeps every existing
        # test on the "run it here" path.
        self._interactive_session = interactive_session
        self._supports_handoff = supports_handoff
        self._handoff_result = handoff_result
        self.handoffs: list[tuple[list[str], float]] = []
        # The parked-pane reset: what pane_reset_command answers (None = this
        # shell has no scripted reset) and every (shell_image, notice) asked.
        self._pane_reset = pane_reset
        self.pane_resets: list[tuple[str, str]] = []
        self.psmux_sessions: set[str] = set()
        self.psmux_launches: list[list[str]] = []
        self.attached_psmux: list[tuple] = []
        self.attached_profiles: list[str | None] = []
        self.moved: list[tuple] = []
        self.nudged: list[list] = []
        self.titles_set: list[tuple] = []
        self.flashed: list = []
        self.focused: list = []

    def _register_window(self, title: str) -> None:
        """Simulate the launched window becoming visible, so a launch->tile
        flow within one test resolves the handle without a real sleep."""
        self._windows[title] = self._next_handle
        self._next_handle += 1

    def set_dpi_aware(self) -> None:
        self.dpi_aware_calls += 1

    def list_monitors(self):
        return self._monitors

    def find_window(self, title: str, mode: str = "exact"):
        return self._windows.get(title)

    def move_window(self, handle, rect) -> None:
        self.moved.append((handle, rect))

    def launch_terminal(self, opts: TerminalLaunchOpts) -> None:
        self.launched_terminals.append(opts)
        self._register_window(opts.title)

    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None:
        self.launched_vscode.append(opts)
        self._register_window(get_leaf_name(opts.dir))

    def snapshot_windows(self):
        return self._windows

    def launch_psmux_session(self, windows) -> dict[str, str]:
        self.launched_psmux.extend(windows)
        self.psmux_launches.append([w.window_name for w in windows])
        for w in windows:
            if w.window_name in self._psmux_launch_failures:
                self._psmux_launch_failures.discard(w.window_name)
                continue
            self.psmux_sessions.add(w.window_name)
        return {}

    def attach_psmux(
        self, session_name, title, color=None, config_path=None, profile=None
    ) -> None:
        self.attached_psmux.append((session_name, title, color, config_path))
        self.attached_profiles.append(profile)

    def supports_psmux(self) -> bool:
        return self._supports_psmux

    def supports_hotkey(self) -> bool:
        return self._supports_hotkey

    def supports_wt_keybindings(self) -> bool:
        return self._supports_wt_keybindings

    def supports_wt_profiles(self) -> bool:
        return self._supports_wt_profiles

    def supports_attach_windows(self) -> bool:
        return self._supports_attach_windows

    def logon_session_is_interactive(self) -> bool:
        return self._interactive_session

    def supports_desktop_handoff(self) -> bool:
        return self._supports_handoff

    def run_on_desktop(self, argv, *, timeout_s) -> HandoffResult:
        """Record the hand-off instead of writing a real scheduled task. The
        canned result stands in for whatever the desktop copy decided."""
        self.handoffs.append((list(argv), timeout_s))
        if self._handoff_result is None:
            return HandoffResult(rc=0)
        return self._handoff_result

    def pane_reset_command(self, shell_image: str, notice: str) -> str | None:
        self.pane_resets.append((shell_image, notice))
        return self._pane_reset

    def supports_attention_signals(self) -> bool:
        return self._supports_attention

    def supports_window_nudge(self) -> bool:
        return self._supports_nudge

    def nudge_windows(self, handles) -> int:
        """Record the batch instead of resizing real windows. `nudge_error`
        stands in for a window that died between the snapshot and the resize."""
        self.nudged.append(list(handles))
        if self._nudge_error is not None:
            raise self._nudge_error
        return len(handles)

    def supports_window_close(self) -> bool:
        return self._supports_close

    def close_window(self, handle) -> bool:
        """Record the WM_CLOSE instead of posting one, and drop the window from
        the snapshot -- what a real close does, so a later lookup misses it.
        `close_error` stands in for a handle that died first."""
        if self._close_error is not None:
            raise self._close_error
        self.closed.append(handle)
        for t, h in list(self._windows.items()):
            if h == handle:
                del self._windows[t]
                break
        return True

    def supports_process_scan(self) -> bool:
        return self._supports_scan

    def process_cmdlines(self, names: list[str]) -> list[str]:
        """Serve the canned command lines. `scan_error` stands in for a scan
        that could not run at all -- the "do not act on this" case."""
        self.scanned.append(list(names))
        if self._scan_error is not None:
            raise self._scan_error
        return list(self._cmdlines)

    def set_window_title(self, handle, title: str) -> bool:
        self.titles_set.append((handle, title))
        # Mirror the retitle into the snapshot so the next tick sees it --
        # what a real window manager does, and what idempotency tests need.
        for t, h in list(self._windows.items()):
            if h == handle:
                del self._windows[t]
                self._windows[title] = h
                break
        return True

    def flash_window(self, handle) -> bool:
        self.flashed.append(handle)
        return True

    def focus_window(self, handle) -> bool:
        self.focused.append(handle)
        return True


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_platform(monkeypatch):
    fp = FakePlatform()
    monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
    return fp
