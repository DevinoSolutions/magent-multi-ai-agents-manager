"""``console.human_at_console`` -- the ONE "a person is typing here" check.

Every place magent decides to prompt, or to start something only a person can
finish (a browser Approve), asks this and nothing else. Windows' ``isatty()``
cannot be that check: it answers True for NUL -- a character device -- so a
command run with ``stdin=DEVNULL``, ``< NUL``, from Task Scheduler or from a
detached daemon looked like a person at a terminal (measured: ``magent node
add`` under stdin=NUL started ``claude setup-token`` and opened a browser on
the desktop). The children below are REAL processes: the handle a child
inherits is the property under test, and no monkeypatch can fake one.
"""

from __future__ import annotations

import io
import subprocess
import sys
from typing import IO

import pytest

from magent import console

_ASK_BOTH = (
    "from magent import console; from magent.cli import node_cmd, picker; "
    "print(console.human_at_console(), node_cmd._can_approve(), "
    "picker.raw_mode_available())"
)


def _child(stdin: int | IO[bytes]) -> str:
    r = subprocess.run(
        [sys.executable, "-c", _ASK_BOTH],
        stdin=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return r.stdout.strip()


class TestNoPersonBehindARedirectedStdin:
    def test_the_null_device_is_nobody(self):
        """The exact shape of the live finding: stdin=NUL on Windows reports
        isatty() True, and must still answer False everywhere."""
        assert _child(subprocess.DEVNULL) == "False False False"

    def test_a_pipe_is_nobody(self):
        assert _child(subprocess.PIPE) == "False False False"

    def test_a_file_is_nobody(self, tmp_path):
        src = tmp_path / "in.txt"
        src.write_text("y\n", encoding="utf-8")
        with src.open("rb") as fh:
            assert _child(fh) == "False False False"

    @pytest.mark.skipif(sys.platform != "win32", reason="NUL is a tty only on Windows")
    def test_windows_really_does_call_nul_a_tty(self):
        """Why ``isatty`` alone is wrong: pinned, so the premise above cannot
        silently stop being true and leave this suite testing nothing."""
        r = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.isatty())"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert r.stdout.strip() == "True"


class TestAPersonsConsoleIsSomebody:
    @pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NEW_CONSOLE")
    def test_a_real_console_on_stdin_is_a_person(self, tmp_path):
        """The positive half, so the check cannot pass by always saying no:
        a child given its own (hidden) console has a real console input
        handle. stdout goes to a file, so only the stdin answer is read."""
        out = tmp_path / "out.txt"
        probe = (
            "from magent import console; from magent.cli import node_cmd; "
            f"open({str(out)!r}, 'w').write("
            "f'{console.human_at_console()} {node_cmd._can_approve()}')"
        )
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE: a real console, never a visible window
        subprocess.run(
            [sys.executable, "-c", probe],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=si,
            timeout=90,
            check=True,
        )
        assert out.read_text(encoding="utf-8") == "True True"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX: isatty is the answer")
    def test_on_posix_a_tty_is_a_person(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        assert console.human_at_console() is True


class TestAStdinThatCannotAnswerIsNobody:
    def test_a_stream_with_no_handle(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert console.human_at_console() is False

    def test_a_stream_whose_isatty_raises(self, monkeypatch):
        class _Detached:
            def isatty(self) -> bool:
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(sys, "stdin", _Detached())
        assert console.human_at_console() is False

    def test_no_stdin_at_all(self, monkeypatch):
        """pythonw, a service: ``sys.stdin`` is None."""
        monkeypatch.setattr(sys, "stdin", None)
        assert console.human_at_console() is False

    def test_a_tty_that_is_not_a_console(self, monkeypatch):
        """Windows NUL, in-process: isatty says yes, the console says no."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(console, "stdin_is_console", lambda: False)
        assert console.human_at_console() is False
