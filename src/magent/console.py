"""Is a person at this console? The ONE answer to that question.

Every place magent decides to prompt -- or to start something only a person
can finish, like the browser Approve behind ``claude setup-token`` -- asks
``human_at_console()`` and nothing else, so a command run from a script, a
scheduler or a detached daemon can never mistake itself for a person.

``sys.stdin.isatty()`` alone is not that answer on Windows: it is True for the
NUL device, which is a character device and not a console. So ``< NUL``,
``stdin=DEVNULL``, Task Scheduler, services and magent's own detached children
all "had a tty" (measured: ``magent node add`` run with stdin=NUL started
``claude setup-token`` and opened a browser on the desktop; v3.19.0's ``--go``
checklist blocked forever on ``getwch`` under the same NUL). ``GetConsoleMode``
succeeds only on a real console input handle -- NUL, pipes and files all fail
it. On POSIX ``/dev/null`` is not a tty, and ``isatty`` stays the whole answer.
"""

from __future__ import annotations

import sys


def stdin_is_console() -> bool:
    """On Windows, whether stdin is a real console input handle (see the
    module docstring); on POSIX always True, because ``isatty`` already
    answered there. Anything that cannot be asked counts as no console."""
    if sys.platform != "win32":
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def human_at_console() -> bool:
    """True only when a person can type into this process's stdin: a tty,
    and on Windows a real console. No stdin (pythonw, a service), a stream
    with no handle, or one that cannot answer is nobody."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
    except (OSError, ValueError, AttributeError):
        return False
    return stdin_is_console()
