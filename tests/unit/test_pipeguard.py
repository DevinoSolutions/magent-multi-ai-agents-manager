"""``pipeguard`` treats an EINVAL/EPIPE as a closed pipe only when it came from
a std stream write -- never by errno alone."""

from __future__ import annotations

import errno
import io
import sys

import pytest

from magent.cli import pipeguard


class _Dead(io.StringIO):
    def __init__(self, err):
        super().__init__()
        self._err = err

    def write(self, text):
        raise self._err

    def fileno(self):
        raise OSError("no fd")


@pytest.mark.parametrize(
    "exc",
    [BrokenPipeError(errno.EPIPE, "pipe"), OSError(errno.EINVAL, "Invalid argument")],
)
def test_a_failed_stdout_write_exits_one_quietly(monkeypatch, exc):
    silenced = []
    # _silence dup2s over a real fd: never let a unit test aim it at pytest's.
    monkeypatch.setattr(pipeguard, "_silence", silenced.append)
    monkeypatch.setattr(sys, "stdout", _Dead(exc))
    with pytest.raises(SystemExit) as info:
        pipeguard.run_guarded(lambda: sys.stdout.write("hi"))
    assert info.value.code == 1
    assert len(silenced) == 2


def test_einval_from_elsewhere_still_propagates():
    def work():
        raise OSError(errno.EINVAL, "Invalid argument")

    with pytest.raises(OSError, match="Invalid argument"):
        pipeguard.run_guarded(work)


def test_a_healthy_run_returns_and_restores_the_streams():
    before = (sys.stdout, sys.stderr)
    assert pipeguard.run_guarded(lambda: 7) == 7
    assert (sys.stdout, sys.stderr) == before
