import json
import os

import pytest
from click.testing import CliRunner

from magent import agent_state, env, log
from magent.grid import MonitorRect
from magent.platform import (
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    VSCodeLaunchOpts,
)
from magent.titles import get_leaf_name


@pytest.fixture(autouse=True)
def _isolate_magent_home(tmp_path, monkeypatch):
    """Every test's log/heartbeat/agent-state/env-file reads and writes land
    under tmp_path, never the real ~/.magent -- autouse so no test can
    forget it (and so a developer machine's live agent-state records or
    ~/.magent/.env can't leak into assertions)."""
    monkeypatch.setattr(log, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(log, "HEARTBEAT_DIR", tmp_path / "hb")
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "agent-state")
    monkeypatch.setattr(env, "ENV_FILE", tmp_path / "env-file")
    monkeypatch.setattr(env, "_cached_env", None)
    # Isolating ENV_FILE (above) is not enough: an exported process-env
    # MAGENT_* var (a dev shell's real MAGENT_SENTRY_DSN) is still read by
    # get_env(), and on 2026-07-07 that leaked fake test errors to prod Sentry
    # (MAGENT-1..4). Strip every MAGENT_* key so no test can init real
    # Sentry or see ambient config -- unknown keys swept too, matching env.py's
    # own closed-schema scan.
    for _key in list(os.environ):
        if _key.upper().startswith("MAGENT_"):
            monkeypatch.delenv(_key, raising=False)
    log.reset_logging()
    yield
    log.reset_logging()


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
        self._next_handle = 1
        self.dpi_aware_calls = 0
        self.launched_terminals: list[TerminalLaunchOpts] = []
        self.launched_vscode: list[VSCodeLaunchOpts] = []
        self.launched_psmux: list[PsmuxWindowOpts] = []
        self.attached_psmux: list[tuple] = []
        self.moved: list[tuple] = []
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

    def launch_psmux_session(self, windows) -> None:
        self.launched_psmux.extend(windows)

    def attach_psmux(self, session_name, title, color=None, config_path=None) -> None:
        self.attached_psmux.append((session_name, title, color, config_path))

    def supports_psmux(self) -> bool:
        return self._supports_psmux

    def supports_attention_signals(self) -> bool:
        return self._supports_attention

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
