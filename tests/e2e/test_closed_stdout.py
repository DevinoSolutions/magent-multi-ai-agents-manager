"""A closed stdout pipe ends magent quietly (Sentry MAGENT-1).

``magent status | head`` breaks the pipe; Windows reports that as
``OSError [Errno 22]`` rather than ``BrokenPipeError`` and it used to escape as
a traceback. Real subprocess, real pipe, HOME redirected by ``tests/conftest.py``.
"""

from __future__ import annotations

import subprocess
import sys

# Prints far more than a pipe buffer holds, so the child is still writing when
# the reader is gone -- whatever the command, the first write after close fails.
_CHILD = """
import sys
from magent.cli import main
import click

@main.command("spew")
def spew():
    for _ in range(20000):
        click.echo("x" * 200)

main(["spew"])
"""


def test_a_closed_stdout_pipe_exits_one_without_a_traceback():
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.read(10)
    proc.stdout.close()
    stderr = proc.stderr.read()
    rc = proc.wait(timeout=60)
    proc.stderr.close()
    assert rc == 1
    assert "Traceback" not in stderr
    assert "Errno" not in stderr
    assert "BrokenPipe" not in stderr


def test_a_process_with_no_std_streams_still_runs():
    """A detached, console-less daemon has sys.stdout/stderr = None; the guard
    must leave that alone (it broke every real daemon spawn on Windows CI)."""
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "from magent.cli import main\n"
        "main(['--version'])\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    assert done.returncode == 0
    assert b"Traceback" not in done.stderr
