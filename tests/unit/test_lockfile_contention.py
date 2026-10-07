"""Windows lock contention surfaces as ``LockHeld``, never a raw PermissionError.

Sentry MAGENT-2: with two ``magent serve`` processes alive, the contender's
``open(path, "w")`` failed with EACCES before ``msvcrt.locking`` ever ran --
truncating a file whose byte range another process has locked is refused, and a
path the holder just unlinked can be delete-pending. That escaped as
PermissionError -> ``log.exception`` -> a Sentry event, instead of the
``LockHeld`` the supervisor logs at debug.

HOME is redirected for every test by ``tests/conftest.py``, so the holder
subprocess and this process agree on a tmp ``~/.magent`` without a patch.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from magent import lockfile
from magent.lockfile import LockHeld, exclusive_lock

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the truncating open only fails under msvcrt locks"
)

_HOLDER = """
import sys, time
from magent.lockfile import exclusive_lock
with exclusive_lock("contend"):
    print("held", flush=True)
    sys.stdin.readline()
"""


def test_a_real_contender_sees_lock_held_not_permission_error():
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        for _ in range(3):
            with pytest.raises(LockHeld), exclusive_lock("contend"):
                pass
    finally:
        holder.communicate("\n", timeout=30)
    with exclusive_lock("contend"):
        pass


_CHURN = """
import sys, time
from magent.lockfile import LockHeld, exclusive_lock
print("go", flush=True)
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    try:
        with exclusive_lock("churn"):
            pass
    except LockHeld:
        pass
"""


def test_contenders_against_a_churning_holder_never_see_permission_error():
    """The holder's close-then-unlink leaves the path delete-pending for an
    instant; a contender opening then used to get EACCES."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _CHURN, "6"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "go"
        outcomes = {"held": 0, "took": 0}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with exclusive_lock("churn"):
                    outcomes["took"] += 1
            except LockHeld:
                outcomes["held"] += 1
        assert sum(outcomes.values()) > 100
    finally:
        holder.communicate(timeout=30)


def test_a_permission_error_from_the_open_is_a_held_lock(monkeypatch):
    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lockfile.os, "open", refuse)
    with pytest.raises(LockHeld), exclusive_lock("contend"):
        pass


def test_other_open_errors_still_raise(monkeypatch):
    def broken(*_a, **_k):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(lockfile.os, "open", broken)
    with pytest.raises(OSError) as info, exclusive_lock("contend"):
        pass
    assert not isinstance(info.value, LockHeld)
