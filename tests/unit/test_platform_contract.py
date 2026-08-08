import sys

import pytest

from magent.platform import Platform
from magent.platform.linux import LinuxPlatform
from magent.platform.macos import MacOSPlatform


class _Bare(Platform):
    """Minimal concrete subclass -- exercises only the ABC's own defaults
    (snapshot_windows/launch_psmux_session), not a real platform backend."""

    def set_dpi_aware(self) -> None:
        pass

    def list_monitors(self):
        return []

    def find_window(self, title: str, mode: str = "exact"):
        return None

    def move_window(self, handle, rect) -> None:
        pass

    def launch_terminal(self, opts) -> None:
        pass

    def launch_vscode(self, opts) -> None:
        pass


def test_fake_platform_is_a_platform(fake_platform):
    assert isinstance(fake_platform, Platform)
    monitors = fake_platform.list_monitors()
    assert len(monitors) >= 1
    assert any(m.is_primary for m in monitors)


def test_base_snapshot_windows_default_empty():
    assert _Bare().snapshot_windows() == {}


def test_base_launch_psmux_raises():
    with pytest.raises(NotImplementedError):
        _Bare().launch_psmux_session([])


# --- Capability truth table (R8) --------------------------------------------
# psmux/hotkey are Windows-only today. The ABC's own defaults cover any
# subclass that implements no backend for them (_Bare) as well as the two
# real non-Windows backends -- both import cleanly on any OS (no ctypes/windll
# at import time), unlike WindowsPlatform below.
_DEFAULT_BACKENDS = [_Bare, LinuxPlatform, MacOSPlatform]


@pytest.mark.parametrize("platform_cls", _DEFAULT_BACKENDS)
def test_default_supports_psmux_false(platform_cls):
    assert platform_cls().supports_psmux() is False


@pytest.mark.parametrize("platform_cls", _DEFAULT_BACKENDS)
def test_default_supports_hotkey_false(platform_cls):
    assert platform_cls().supports_hotkey() is False


@pytest.mark.parametrize("platform_cls", _DEFAULT_BACKENDS)
def test_default_supports_window_nudge_false(platform_cls):
    assert platform_cls().supports_window_nudge() is False


@pytest.mark.parametrize("platform_cls", _DEFAULT_BACKENDS)
def test_default_nudge_windows_is_a_noop(platform_cls):
    # A capability the caller gates on supports_window_nudge(); calling it
    # anyway must be harmless, not an exception.
    assert platform_cls().nudge_windows([object(), object()]) == 0


@pytest.mark.parametrize("platform_cls", _DEFAULT_BACKENDS)
def test_default_attach_psmux_raises(platform_cls):
    with pytest.raises(NotImplementedError, match="psmux"):
        platform_cls().attach_psmux("s", "t")


@pytest.mark.parametrize("platform_cls", [LinuxPlatform, MacOSPlatform])
def test_default_launch_psmux_session_raises(platform_cls):
    with pytest.raises(NotImplementedError):
        platform_cls().launch_psmux_session([])


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsCapabilities:
    def test_supports_psmux_true(self):
        from magent.platform.windows import WindowsPlatform

        assert WindowsPlatform().supports_psmux() is True

    def test_supports_hotkey_true(self):
        from magent.platform.windows import WindowsPlatform

        assert WindowsPlatform().supports_hotkey() is True

    def test_supports_window_nudge_true(self):
        from magent.platform.windows import WindowsPlatform

        assert WindowsPlatform().supports_window_nudge() is True


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsNudge:
    """The geometry nudge resizes by a cell-crossing delta, settles once for
    the whole batch, and restores every window's exact original rect -- the
    only lever that makes psmux 3.3.6 adopt this client's size."""

    def _driver(self, monkeypatch, rects, *, move_fails_for=()):
        """Replace the three user32 calls with recorders. `rects` maps handle
        -> (left, top, right, bottom) as GetWindowRect would fill it."""
        from magent.platform import windows as win_mod

        moves: list[tuple] = []
        sleeps: list[float] = []

        def fake_get_rect(handle, out):
            if handle not in rects:
                return 0
            left, top, right, bottom = rects[handle]
            out._obj.left, out._obj.top = left, top
            out._obj.right, out._obj.bottom = right, bottom
            return 1

        def fake_move(handle, x, y, w, h, repaint):
            if handle in move_fails_for:
                raise OSError("invalid window handle")
            moves.append((handle, x, y, w, h))
            return 1

        monkeypatch.setattr(win_mod.user32, "GetWindowRect", fake_get_rect)
        monkeypatch.setattr(win_mod.user32, "MoveWindow", fake_move)
        monkeypatch.setattr(win_mod.time, "sleep", sleeps.append)
        return moves, sleeps

    def test_shrinks_then_restores_the_exact_rect(self, monkeypatch):
        from magent.platform.windows import _NUDGE_DELTA_PX, WindowsPlatform

        moves, sleeps = self._driver(monkeypatch, {1: (100, 200, 900, 800)})
        assert WindowsPlatform().nudge_windows([1]) == 1
        assert moves == [
            (1, 100, 200, 800, 600 - _NUDGE_DELTA_PX),
            (1, 100, 200, 800, 600),
        ]
        # The delta must be big enough to cross a character cell -- a 1px
        # nudge can resize the window without changing the reported grid.
        assert _NUDGE_DELTA_PX >= 20
        assert len(sleeps) == 1  # one shared settle for the whole batch

    def test_batch_shares_one_settle(self, monkeypatch):
        from magent.platform.windows import WindowsPlatform

        moves, sleeps = self._driver(
            monkeypatch, {1: (0, 0, 800, 600), 2: (800, 0, 1600, 600)}
        )
        assert WindowsPlatform().nudge_windows([1, 2]) == 2
        # Both shrink, then both restore -- so the settle covers every window.
        assert [m[0] for m in moves] == [1, 2, 1, 2]
        assert len(sleeps) == 1

    def test_dead_handle_is_skipped_not_fatal(self, monkeypatch):
        from magent.platform.windows import WindowsPlatform

        moves, _ = self._driver(monkeypatch, {1: (0, 0, 800, 600)})  # 2 has no rect
        assert WindowsPlatform().nudge_windows([2, 1]) == 1
        assert [m[0] for m in moves] == [1, 1]

    def test_a_window_dying_mid_nudge_does_not_raise(self, monkeypatch):
        from magent.platform.windows import WindowsPlatform

        moves, _ = self._driver(
            monkeypatch,
            {1: (0, 0, 800, 600), 2: (800, 0, 1600, 600)},
            move_fails_for=(2,),
        )
        assert WindowsPlatform().nudge_windows([1, 2]) == 1
        assert [m[0] for m in moves] == [1, 1]

    def test_nothing_to_nudge_skips_the_settle(self, monkeypatch):
        from magent.platform.windows import WindowsPlatform

        moves, sleeps = self._driver(monkeypatch, {})
        assert WindowsPlatform().nudge_windows([1]) == 0
        assert moves == [] and sleeps == []


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsLaunchTerminal:
    """TF-W-001: a missing `wt` must surface as a typed, actionable error rather
    than a raw FileNotFoundError traceback. launch_terminal catches the Popen
    FileNotFoundError and re-raises TerminalNotFoundError carrying the winget
    install hint, preserving the original error as its cause."""

    def test_missing_wt_raises_actionable_terminal_not_found(self, monkeypatch):
        from magent.platform import TerminalLaunchOpts, TerminalNotFoundError
        from magent.platform.windows import WindowsPlatform

        def _no_wt(*_args, **_kwargs):
            raise FileNotFoundError(2, "The system cannot find the file specified")

        monkeypatch.setattr("magent.platform.windows.subprocess.Popen", _no_wt)

        with pytest.raises(TerminalNotFoundError) as excinfo:
            WindowsPlatform().launch_terminal(
                TerminalLaunchOpts(title="magent:proj", cwd=".", command="claude")
            )

        assert "winget install Microsoft.WindowsTerminal" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)


# The status-right hint, restated rather than imported: the point of the pin is
# that a restyle in psmux.py has to be a deliberate edit here too.
_EXPECTED_HINT = (
    "#[bold,fg=cyan] F1 #[default]Proj. Picker   "
    "#[bold,fg=cyan] F2 #[default]</> VS Code "
)

# ...and what the launch path sends on a machine with no VS Code: the F1 half
# alone. F2 is the hotkey listener's key and the listener needs `code` on PATH.
_EXPECTED_HINT_F1_ONLY = "#[bold,fg=cyan] F1 #[default]Proj. Picker "


def _drive_bring_up(
    monkeypatch,
    *,
    popen_error: Exception | None = None,
    code_hint: bool = True,
    windows: list[str] | None = None,
    pane_states: dict[str, list[str]] | None = None,
    pane_probes: list[list[str]] | None = None,
    create_failures: set[str] | None = None,
    envs: list[object] | None = None,
):
    """Drive a real ``launch_psmux_session`` over a fully faked psmux seam.

    Shared by the decoration pins and the send-keys verification pins below:
    both observe the same bring-up, so a change to one can't silently drift
    away from the other. Nothing here reads the ambient PATH or waits on real
    time -- `code_on_path`, `subprocess.Popen`, the pane probe and
    `time.sleep` are all replaced.
    """
    from magent.platform import PsmuxWindowOpts
    from magent.platform.windows import WindowsPlatform

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0

        def wait(self):
            return 0

    def _popen(cmd, **kwargs):
        calls.append(list(cmd))
        if envs is not None:
            envs.append(kwargs.get("env"))
        # has-session must report "down" so the session gets created.
        if "has-session" in cmd:
            proc = _Proc()
            proc.returncode = 1
            proc.wait = lambda: 1
            return proc
        # A session psmux refuses to create ("failed to create session 'X'").
        if "new-session" in cmd and cmd[2] in (create_failures or set()):
            proc = _Proc()
            proc.returncode = 1
            proc.wait = lambda: 1
            return proc
        if popen_error is not None and "bind" in cmd:
            raise popen_error
        return _Proc()

    probes: list[int] = []

    # Per-session pane readings across SUCCESSIVE verification probes:
    # `pane_states[name][i]` is what probe i reports, the last entry repeating
    # forever. The default is a running agent, so every pin that predates
    # send-keys verification sees a healthy batch and no re-send.
    states = dict(pane_states or {})
    seen: dict[str, int] = {}

    def _fake_pane_commands(names, psmux=None):
        if pane_probes is not None:
            pane_probes.append(list(names))
        out = {}
        for n in names:
            readings = states.get(n) or ["claude"]
            out[n] = readings[min(seen.get(n, 0), len(readings) - 1)]
            seen[n] = seen.get(n, 0) + 1
        return out

    monkeypatch.setattr("magent.platform.windows.find_psmux", lambda: "psmux")
    monkeypatch.setattr("magent.platform.windows.subprocess.Popen", _popen)
    monkeypatch.setattr(
        "magent.platform.windows._wait_for_panes_ready", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "magent.platform.windows.pane_current_commands", _fake_pane_commands
    )
    # The inter-batch settle pause is real seconds; nothing here waits on
    # a real process, so it only slows the multi-batch case down.
    monkeypatch.setattr("magent.platform.windows.time.sleep", lambda _s: None)
    # Pinned, never probed: a CI runner may or may not ship `code`, and a
    # pin that reads the runner's PATH pins nothing. `probes` rides along
    # so the once-per-bring-up contract is checkable.
    monkeypatch.setattr(
        "magent.platform.windows.code_on_path",
        lambda: (probes.append(1), code_hint)[1],
    )

    names = windows or ["api"]
    WindowsPlatform().launch_psmux_session(
        [PsmuxWindowOpts(window_name=n, cwd=f"/a/{n}", command="claude") for n in names]
    )
    return calls, probes


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsPsmuxDecoration:
    """Every session created by the launch path advertises the F1/F2 hints in
    its status line, and a failure to do so is cosmetic -- never fatal to the
    bring-up."""

    _run = staticmethod(_drive_bring_up)

    def test_created_session_is_decorated(self, monkeypatch):
        calls, _ = self._run(monkeypatch, code_hint=True)
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] in calls
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right",
            _EXPECTED_HINT,
        ] in calls
        # The hint's width budget travels with the hint, same as the brand's.
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right-length",
            "40",
        ] in calls

    def test_without_code_the_launch_path_advertises_f1_alone(self, monkeypatch):
        # The machine launching IS the machine displaying these windows, so its
        # own PATH is the right authority -- and when `code` isn't on it, the
        # F2 key must not be advertised anywhere in what magent sends.
        calls, _ = self._run(monkeypatch, code_hint=False)
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] in calls
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right",
            _EXPECTED_HINT_F1_ONLY,
        ] in calls
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right-length",
            "22",
        ] in calls
        # The `unbind-key -n F2` retraction is the one command allowed to name
        # the key here (it removes a stale binding rather than advertising one),
        # so it is excluded before the "nothing mentions F2" sweep.
        advertised = [cmd for cmd in calls if "unbind-key" not in cmd]
        assert ["psmux", "-L", "api", "unbind-key", "-n", "F2"] in calls
        flat = " ".join(arg for cmd in advertised for arg in cmd)
        assert "</>" not in flat
        assert "VS Code" not in flat
        assert "F2" not in flat

    def test_code_is_probed_once_per_bring_up_not_per_window(self, monkeypatch):
        # Two batches' worth of sessions (_BRING_UP_BATCH is 5), so a probe
        # inside the batch loop -- or inside _decorate_batch -- would show up
        # as more than one call.
        names = [f"p{i}" for i in range(7)]
        calls, probes = self._run(monkeypatch, windows=names)
        assert len(probes) == 1
        # ...and every session still got the hint the single probe decided.
        for n in names:
            assert [
                "psmux",
                "-L",
                n,
                "set",
                "-g",
                "status-right",
                _EXPECTED_HINT,
            ] in calls

    def test_created_session_is_branded(self, monkeypatch):
        # decoration_argv is the single source, so the creation batch picks the
        # brand up with no call-site change -- this pins that it actually does.
        calls, _ = self._run(monkeypatch)
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left",
            "#[bold,fg=green] magent #[default]",
        ] in calls
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-left-length",
            "10",
        ] in calls

    def test_decoration_happens_after_the_agent_is_sent(self, monkeypatch):
        calls, _ = self._run(monkeypatch)
        sent = next(i for i, c in enumerate(calls) if "send-keys" in c)
        bound = next(i for i, c in enumerate(calls) if "bind" in c)
        assert bound > sent

    def test_decoration_failure_never_fails_the_bring_up(self, monkeypatch):
        # No raise: a status bar must never cost the user their sessions.
        self._run(monkeypatch, popen_error=OSError("spawn failed"))


def _sends_for(calls: list[list[str]], name: str) -> list[list[str]]:
    """Every send-keys argv aimed at session ``name``."""
    return [c for c in calls if "send-keys" in c and c[2] == name]


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsSendKeysVerification:
    """A fresh session is a bare pwsh until send-keys types the agent command
    into it, and under a spawn storm PSReadLine can flush that input away
    during shell init -- leaving a pane parked at a prompt that still passes
    every liveness probe (session up, pane alive), so nothing downstream
    notices. The bring-up therefore probes each batch's panes after the send
    and re-types the command into the ones still resting at a shell.

    Drives the same `_drive_bring_up` harness the decoration pins use, so the
    two contracts observe one bring-up and cannot drift apart.
    """

    def test_healthy_batch_is_sent_once(self, monkeypatch):
        calls, _ = _drive_bring_up(monkeypatch, pane_states={"api": ["claude"]})
        assert len(_sends_for(calls, "api")) == 1

    def test_a_pane_still_at_pwsh_is_re_sent_once_and_only_it(self, monkeypatch):
        # `web` swallowed its keystrokes and sits at pwsh on the first probe,
        # then picks the command up after the re-send.
        calls, _ = _drive_bring_up(
            monkeypatch,
            windows=["api", "web"],
            pane_states={"api": ["claude"], "web": ["pwsh", "claude"]},
        )
        assert len(_sends_for(calls, "web")) == 2
        assert len(_sends_for(calls, "api")) == 1

    def test_a_pane_that_never_recovers_stops_at_the_bound(self, monkeypatch, caplog):
        import logging

        from magent.platform.windows import _SEND_MAX_ATTEMPTS

        with caplog.at_level(logging.WARNING, logger="magent.platform"):
            calls, _ = _drive_bring_up(monkeypatch, pane_states={"api": ["pwsh"]})

        # Bounded: the original send plus re-sends, never an unbounded retry.
        assert len(_sends_for(calls, "api")) == _SEND_MAX_ATTEMPTS
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("api" in r.getMessage() for r in errors)
        # ...and the bring-up still finished: no raise, and the session was
        # still decorated -- one stuck pane must not cost the whole wave.
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] in calls

    def test_a_re_send_is_logged_loudly(self, monkeypatch, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="magent.platform"):
            _drive_bring_up(monkeypatch, pane_states={"api": ["pwsh", "claude"]})

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("api" in r.getMessage() for r in warnings)

    def test_a_pane_running_the_agent_is_never_re_sent(self, monkeypatch):
        # THE dangerous edge: a re-send into a live agent types the command
        # text into its input box. The immediately-before-re-send probe is the
        # only guard, so every non-shell reading must be left alone -- `cmd`
        # (an agent still booting through its .cmd shim) included.
        calls, _ = _drive_bring_up(
            monkeypatch,
            windows=["api", "web"],
            pane_states={"api": ["cmd"], "web": ["claude"]},
        )
        assert len(_sends_for(calls, "api")) == 1
        assert len(_sends_for(calls, "web")) == 1

    def test_an_unreadable_pane_is_never_re_sent(self, monkeypatch):
        # A probe that could not answer is not evidence of a dead pane --
        # same posture as psmux.agent_idle, which is False on an empty read.
        calls, _ = _drive_bring_up(monkeypatch, pane_states={"api": [""]})
        assert len(_sends_for(calls, "api")) == 1

    def test_the_batch_is_probed_in_one_fan_out_not_per_session(self, monkeypatch):
        # One probe call carrying every pending name -- not one call per
        # session, which would serialize a psmux round-trip per pane.
        probed: list[list[str]] = []
        names = [f"p{i}" for i in range(3)]
        _drive_bring_up(monkeypatch, windows=names, pane_probes=probed)
        assert probed == [names]

    def test_healthy_sessions_drop_out_of_later_probes(self, monkeypatch):
        probed: list[list[str]] = []
        _drive_bring_up(
            monkeypatch,
            windows=["api", "web"],
            pane_states={"api": ["claude"], "web": ["pwsh", "claude"]},
            pane_probes=probed,
        )
        assert probed == [["api", "web"], ["web"]]

    def test_the_re_send_is_byte_identical_to_the_original(self, monkeypatch):
        # The retry must retype exactly what was lost -- a divergent argv here
        # would resurrect the pane with a different command than configured.
        calls, _ = _drive_bring_up(monkeypatch, pane_states={"api": ["pwsh"]})
        sends = _sends_for(calls, "api")
        assert all(c == sends[0] for c in sends[1:])
        assert sends[0][-2:] == ["cmd /c claude", "Enter"]


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsBringUpContainsCreationFailures:
    """One window psmux refuses must not cost the wave its remaining ones.

    Live repro: ``psmux: failed to create session 'EmailSESFix'`` (rc 1) raised
    ``CalledProcessError`` out of ``launch_psmux_session``, so `magent up` and
    the interactive menu's `u` ended in a traceback with every later batch
    abandoned -- the user's "error towards the end".
    """

    def test_a_refused_session_does_not_raise(self, monkeypatch):
        # No pytest.raises: the contract is that nothing escapes at all.
        _drive_bring_up(monkeypatch, windows=["api"], create_failures={"api"})

    def test_the_other_sessions_in_the_batch_still_get_their_agent(self, monkeypatch):
        calls, _ = _drive_bring_up(
            monkeypatch, windows=["api", "web"], create_failures={"api"}
        )
        assert len(_sends_for(calls, "web")) == 1
        # ...and the refused one is never sent keys or decorated: there is no
        # session behind it to receive them.
        assert _sends_for(calls, "api") == []
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] not in calls
        assert ["psmux", "-L", "web", "bind", "-n", "F1", "detach-client"] in calls

    def test_a_later_batch_still_runs(self, monkeypatch):
        from magent.platform.windows import _BRING_UP_BATCH

        names = [f"p{i}" for i in range(_BRING_UP_BATCH + 2)]
        calls, _ = _drive_bring_up(
            monkeypatch, windows=names, create_failures={names[0]}
        )
        # The failure is in batch 1; the last name lives in batch 2 and must
        # still have been created and sent its command.
        assert len(_sends_for(calls, names[-1])) == 1

    def test_a_whole_failed_batch_is_skipped_not_fatal(self, monkeypatch):
        calls, _ = _drive_bring_up(
            monkeypatch, windows=["api", "web"], create_failures={"api", "web"}
        )
        assert [c for c in calls if "send-keys" in c] == []

    def test_the_refusal_is_logged_with_the_session_name(self, monkeypatch, caplog):
        import logging

        with caplog.at_level(logging.ERROR, logger="magent.platform"):
            _drive_bring_up(monkeypatch, windows=["api"], create_failures={"api"})
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("api" in r.getMessage() for r in errors)


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsBringUpPsmuxInvocation:
    """The launch path's psmux children: truthful probes, and an environment
    with the multiplexer's nesting markers stripped."""

    def test_the_dedupe_probe_targets_the_session(self, monkeypatch):
        calls, _ = _drive_bring_up(monkeypatch, windows=["api"])
        probes = [c for c in calls if "has-session" in c]
        # A bare has-session exits 0 for a socket with no server, so this
        # dedupe would skip creating every session on a cold machine.
        assert probes == [["psmux", "-L", "api", "has-session", "-t", "api"]]

    def test_every_child_runs_without_the_nesting_markers(self, monkeypatch):
        seen: list[object] = []
        monkeypatch.setenv("PSMUX_SESSION", "api")
        monkeypatch.setenv("TMUX", "/tmp/psmux-1/default,1,0")
        _drive_bring_up(
            monkeypatch,
            windows=["api"],
            pane_states={"api": ["pwsh", "claude"]},  # forces a re-send too
            envs=seen,
        )
        assert seen, "nothing was spawned"
        for env in seen:
            assert isinstance(env, dict)
            assert not [k for k in env if k.upper().startswith(("PSMUX", "TMUX"))]


# --- find_window mode contract (LS-B-005) -----------------------------------
# mode is a Literal["exact", "contains"]; a typo'd mode must fail loudly
# instead of silently reporting "not found".


@pytest.mark.parametrize("platform_cls", [LinuxPlatform, MacOSPlatform])
def test_find_window_unknown_mode_raises(platform_cls):
    with pytest.raises(ValueError):
        platform_cls().find_window("t", mode="bogus")  # type: ignore[arg-type]  # reason: invalid mode passed on purpose to prove it raises


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
def test_find_window_unknown_mode_raises_windows():
    from magent.platform.windows import WindowsPlatform

    with pytest.raises(ValueError):
        WindowsPlatform().find_window("t", mode="bogus")  # type: ignore[arg-type]  # reason: invalid mode passed on purpose to prove it raises
