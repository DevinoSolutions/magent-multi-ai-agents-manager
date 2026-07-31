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


@pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)
class TestWindowsPsmuxDecoration:
    """Every session created by the launch path advertises the F1/F2 hints in
    its status line, and a failure to do so is cosmetic -- never fatal to the
    bring-up."""

    def _run(self, monkeypatch, *, popen_error: Exception | None = None):
        from magent.platform import PsmuxWindowOpts
        from magent.platform.windows import WindowsPlatform

        calls: list[list[str]] = []

        class _Proc:
            returncode = 0

            def wait(self):
                return 0

        def _popen(cmd, **_kwargs):
            calls.append(list(cmd))
            # has-session must report "down" so the session gets created.
            if "has-session" in cmd:
                proc = _Proc()
                proc.returncode = 1
                proc.wait = lambda: 1
                return proc
            if popen_error is not None and "bind" in cmd:
                raise popen_error
            return _Proc()

        monkeypatch.setattr("magent.platform.windows.find_psmux", lambda: "psmux")
        monkeypatch.setattr("magent.platform.windows.subprocess.Popen", _popen)
        monkeypatch.setattr(
            "magent.platform.windows._wait_for_panes_ready", lambda *a, **k: None
        )

        WindowsPlatform().launch_psmux_session(
            [PsmuxWindowOpts(window_name="api", cwd="/a/api", command="claude")]
        )
        return calls

    def test_created_session_is_decorated(self, monkeypatch):
        calls = self._run(monkeypatch)
        assert ["psmux", "-L", "api", "bind", "-n", "F1", "detach-client"] in calls
        assert [
            "psmux",
            "-L",
            "api",
            "set",
            "-g",
            "status-right",
            " F1 picker  F2 code ",
        ] in calls

    def test_created_session_is_branded(self, monkeypatch):
        # decoration_argv is the single source, so the creation batch picks the
        # brand up with no call-site change -- this pins that it actually does.
        calls = self._run(monkeypatch)
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
        calls = self._run(monkeypatch)
        sent = next(i for i, c in enumerate(calls) if "send-keys" in c)
        bound = next(i for i, c in enumerate(calls) if "bind" in c)
        assert bound > sent

    def test_decoration_failure_never_fails_the_bring_up(self, monkeypatch):
        # No raise: a status bar must never cost the user their sessions.
        self._run(monkeypatch, popen_error=OSError("spawn failed"))


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
