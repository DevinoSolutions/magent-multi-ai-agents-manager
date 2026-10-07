"""Every psmux argv is built in ``psmux.py`` -- and these are the exact shapes.

The pins at the top were written green BEFORE the argv builders moved out of
``platform/windows.py`` and ``cli/session_picker.py``; they hold the argv each
call site hands the OS, byte for byte, so the move is provably a pure move.
The second half pins the named builders ``psmux.py`` now owns.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from magent import psmux
from magent.psmux import PsmuxWindowOpts

_WIN_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="WindowsPlatform binds windll at import"
)


def _opts() -> PsmuxWindowOpts:
    return PsmuxWindowOpts(
        window_name="proj-a", cwd="C:/work/proj a", command="claude --x"
    )


@_WIN_ONLY
class TestWindowsCallSitesKeepTheirArgv:
    def test_send_argv(self):
        from magent.platform import windows

        assert windows._send_argv("P", _opts()) == [
            "P",
            "-L",
            "proj-a",
            "send-keys",
            "-t",
            "proj-a",
            "cmd /c claude --x",
            "Enter",
        ]

    @pytest.mark.parametrize(
        ("color", "tab"),
        [(None, []), ("#112233", ["--tabColor", "#112233"])],
    )
    def test_attach_psmux(self, monkeypatch, color, tab):
        from magent.platform import windows

        seen: list[list[str]] = []
        monkeypatch.setattr(windows, "find_psmux", lambda: "P")
        monkeypatch.setattr(
            windows.subprocess, "Popen", lambda args, **kw: seen.append(list(args))
        )
        windows.WindowsPlatform().attach_psmux("proj-a", "magent: proj-a", color)
        assert seen == [
            [
                "wt",
                "-w",
                "new",
                "--suppressApplicationTitle",
                "--title",
                "magent: proj-a",
                *tab,
                "--",
                "P",
                "-L",
                "proj-a",
                "attach",
            ]
        ]

    def test_new_session(self, monkeypatch):
        from magent.platform import windows

        seen: list[list[str]] = []

        class _Done:
            pass

        monkeypatch.setattr(windows, "find_psmux", lambda: "P")
        monkeypatch.setattr(
            windows,
            "probe_sessions",
            lambda names, p, timeout: dict.fromkeys(names, "absent"),
        )
        monkeypatch.setattr(
            windows, "clear_stale_servers", lambda names, p, timeout: []
        )
        monkeypatch.setattr(windows, "code_on_path", lambda: False)
        monkeypatch.setattr(
            windows,
            "spawn_unjobbed",
            lambda argv, **kw: seen.append(list(argv)) or _Done(),
        )
        # rc 1 = psmux refused: the wave ends without any send/decorate work.
        monkeypatch.setattr(windows, "await_clients", lambda procs, t: [1] * len(procs))
        windows.WindowsPlatform().launch_psmux_session([_opts()])
        assert seen == [
            [
                "P",
                "-L",
                "proj-a",
                "new-session",
                "-d",
                "-s",
                "proj-a",
                "-c",
                "C:/work/proj a",
            ]
        ]


class TestPickerAttachKeepsItsArgv:
    def test_attach_session(self, monkeypatch):
        from magent.cli import session_picker

        calls: list[list[str]] = []
        monkeypatch.setattr(
            session_picker.subprocess, "call", lambda cmd: calls.append(list(cmd)) or 0
        )
        session_picker._attach_session("P", "sess", lambda: None)
        assert calls == [["P", "-L", "sess", "attach"]]


# ---- the named builders psmux.py owns -------------------------------------


class TestPsmuxOwnsTheArgv:
    def test_attach_argv(self):
        assert psmux.attach_argv("P", "s") == ["P", "-L", "s", "attach"]

    def test_new_session_argv(self):
        assert psmux.new_session_argv("P", "s", "C:/d") == [
            "P", "-L", "s", "new-session", "-d", "-s", "s", "-c", "C:/d",
        ]  # fmt: skip

    def test_type_command_argv(self):
        assert psmux.type_command_argv("P", "s", "claude") == [
            "P", "-L", "s", "send-keys", "-t", "s", "cmd /c claude", "Enter",
        ]  # fmt: skip

    def test_send_keys_argv_shapes(self):
        assert psmux.send_keys_argv("P", "s", "a", "b") == [
            "P", "-L", "s", "send-keys", "--", "a", "b",
        ]  # fmt: skip
        assert psmux.send_keys_argv("P", "s", "x", target="t", literal=True) == [
            "P", "-L", "s", "send-keys", "-t", "t", "-l", "--", "x",
        ]  # fmt: skip

    def test_send_keys_uses_the_builder(self, monkeypatch):
        seen: list[list[str]] = []

        def _run(cmd, **kw):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(psmux.subprocess, "run", _run)
        assert psmux.send_keys("s", "Enter", target="s", psmux="P")
        assert seen == [psmux.send_keys_argv("P", "s", "Enter", target="s")]
