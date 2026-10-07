"""End the process quietly when the reader of stdout/stderr has gone away.

``magent status | head`` (or a closed console) breaks the pipe. POSIX raises
``BrokenPipeError`` (EPIPE) for a write into it; Windows raises
``OSError: [Errno 22] Invalid argument`` (EINVAL) for the same event, which
click only special-cases for EPIPE -- so the traceback escaped to Sentry
(MAGENT-1). It is not a fault in magent: the reader left, so there is nobody
to tell.

EINVAL is far too common an errno to swallow by value alone, so the guard
wraps the two std streams in a proxy that records when ITS write/flush raised
one of those errnos; only then is the exception treated as a closed pipe. An
EINVAL out of anything else still propagates. Ending is the Python docs'
recipe: point the stream's fd at ``os.devnull`` so the interpreter's own final
flush cannot raise again, then exit 1.

Lives in cli/ because it owns an exit decision (MD001); stdlib only.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

T = TypeVar("T")
_CLOSED_PIPE_ERRNOS = frozenset({errno.EPIPE, errno.EINVAL})
EXIT_CODE = 1


def _is_closed_pipe(exc: BaseException) -> bool:
    return isinstance(exc, BrokenPipeError) or (
        isinstance(exc, OSError) and exc.errno in _CLOSED_PIPE_ERRNOS
    )


class _Watched:
    """A std stream that notes when a write or flush on it hit a closed pipe.
    Everything else (``isatty``, ``encoding``, ``reconfigure``, ``fileno``...)
    is the real stream's."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.broken = False

    def write(self, text: str) -> int:
        try:
            return self._stream.write(text)
        except OSError as exc:
            self.broken = self.broken or _is_closed_pipe(exc)
            raise

    def flush(self) -> None:
        try:
            self._stream.flush()
        except OSError as exc:
            self.broken = self.broken or _is_closed_pipe(exc)
            raise

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _silence(stream: TextIO) -> None:
    """Point ``stream``'s fd at the null device (its buffered tail then flushes
    harmlessly at interpreter exit)."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, stream.fileno())
        finally:
            os.close(devnull)


def run_guarded(run: Callable[[], T]) -> T:
    """``run()`` with stdout/stderr watched: a closed pipe ends the process
    quietly (exit 1); anything else propagates untouched."""
    real_out, real_err = sys.stdout, sys.stderr
    # A detached/console-less process has NO stdout/stderr (None): leave that
    # alone -- click and the daemons already treat None as "nowhere to write",
    # and a proxy over None would turn that into an AttributeError.
    out = _Watched(real_out) if real_out is not None else None
    err = _Watched(real_err) if real_err is not None else None
    sys.stdout = out if out is not None else real_out
    sys.stderr = err if err is not None else real_err
    try:
        return run()
    except OSError as exc:
        if not (
            _is_closed_pipe(exc) and any(w is not None and w.broken for w in (out, err))
        ):
            raise
        sys.stdout, sys.stderr = real_out, real_err
        for stream in (real_out, real_err):
            if stream is not None:
                _silence(stream)
        raise SystemExit(EXIT_CODE) from None
    finally:
        sys.stdout, sys.stderr = real_out, real_err
