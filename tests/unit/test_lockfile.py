"""Tests for the exclusive lockfile used by daemon startup guards."""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

import pytest

from magent import lockfile
from magent.lockfile import LockHeld, exclusive_lock, lock_path, persistent_lock

_real_open = open


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


class _Handle:
    """A real lock-file handle that can run one scripted step right after it
    is closed -- the instant its holder's lock is let go."""

    def __init__(self, fh):
        self._fh = fh
        self.after_close = None

    def fileno(self):
        return self._fh.fileno()

    def close(self):
        self._fh.close()
        step, self.after_close = self.after_close, None
        if step is not None:
            step()


class _Opens:
    """Stands in for ``open`` inside ``lockfile``. Every lock file is the real
    one; ``after[n]`` runs right after the n-th open (from 0) returns, so
    another taker's move lands at an exact point of an acquire -- no timing."""

    def __init__(self):
        self.handles = []
        self.after = {}

    def __call__(self, *args, **kwargs):
        handle = _Handle(_real_open(*args, **kwargs))
        self.handles.append(handle)
        step = self.after.pop(len(self.handles) - 1, None)
        if step is not None:
            step()
        return handle


@pytest.fixture
def opens(monkeypatch):
    seam = _Opens()
    monkeypatch.setattr(lockfile, "open", seam, raising=False)
    return seam


def _enter(stack, name):
    """Take lock ``name`` into ``stack``: True when taken, False on LockHeld."""
    try:
        stack.enter_context(exclusive_lock(name))
    except LockHeld:
        return False
    return True


class TestExclusiveLock:
    def test_acquires_and_releases(self):
        with exclusive_lock("test"):
            lock_file = Path.home() / ".magent" / "test.lock"
            assert lock_file.exists()

    def test_lock_file_cleaned_up(self):
        with exclusive_lock("test"):
            pass
        lock_file = Path.home() / ".magent" / "test.lock"
        assert not lock_file.exists()

    def test_second_acquire_raises_lock_held(self):
        with exclusive_lock("test"):  # noqa: SIM117  # reason: outer lock must be held when inner acquire is attempted; collapsing would release it
            with pytest.raises(LockHeld):
                with exclusive_lock("test"):
                    pass

    def test_different_names_do_not_conflict(self):
        with exclusive_lock("alpha"), exclusive_lock("beta"):
            pass

    def test_reacquire_after_release(self):
        with exclusive_lock("test"):
            pass
        with exclusive_lock("test"):
            pass

    def test_cross_thread_exclusion(self):
        acquired = threading.Event()
        blocked = threading.Event()
        released = threading.Event()
        second_ok = threading.Event()

        def holder():
            with exclusive_lock("test"):
                acquired.set()
                blocked.wait(timeout=5)
            released.set()

        def waiter():
            acquired.wait(timeout=5)
            try:
                with exclusive_lock("test"):
                    second_ok.set()
            except LockHeld:
                blocked.set()
                released.wait(timeout=5)
                with exclusive_lock("test"):
                    second_ok.set()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t2.join(timeout=10)
        t1.join(timeout=10)
        assert second_ok.is_set()

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: nested))
        with exclusive_lock("test"):
            assert (nested / ".magent" / "test.lock").exists()

    def test_a_failed_acquire_leaves_the_holders_lock_in_place(self):
        """A contender that lost must not delete the file. On POSIX the holder's
        flock lives on that inode: once the path is gone a THIRD contender
        creates a fresh file, locks it, and runs beside the holder -- two
        daemons."""
        lock_file = Path.home() / ".magent" / "test.lock"
        with exclusive_lock("test"):
            with pytest.raises(LockHeld), exclusive_lock("test"):
                pass
            assert lock_file.exists()
            with pytest.raises(LockHeld), exclusive_lock("test"):
                pass


class TestExclusiveLockAcrossARelease:
    """A taker that meets the holder mid-release. The file is deleted on
    release, so on POSIX a lock can land on a file no path names any more;
    taking that as the lock let a third taker create a fresh file and run
    beside it -- two daemons. Every step is placed by the ``opens`` seam."""

    def test_a_contender_that_opened_before_the_holder_let_go_is_the_only_holder(
        self, opens
    ):
        with (
            contextlib.ExitStack() as holder,
            contextlib.ExitStack() as contender,
            contextlib.ExitStack() as third,
        ):
            assert _enter(holder, "race")
            opens.after[1] = holder.close  # the contender has opened: holder leaves
            assert _enter(contender, "race"), "nobody held it once the holder left"
            assert not _enter(third, "race"), "a third taker got in beside it"

    def test_a_contender_that_opened_before_the_holder_let_go_defers_to_a_new_holder(
        self, opens
    ):
        took = []
        with (
            contextlib.ExitStack() as holder,
            contextlib.ExitStack() as contender,
            contextlib.ExitStack() as third,
            contextlib.ExitStack() as fourth,
        ):
            assert _enter(holder, "race")

            def holder_leaves_and_a_third_takes_it():
                holder.close()
                took.append(_enter(third, "race"))

            opens.after[1] = holder_leaves_and_a_third_takes_it
            assert not _enter(contender, "race"), "the contender got in beside it"
            assert took == [True]
            # The contender left the third taker's file where it was.
            assert lock_path("race").exists()
            assert not _enter(fourth, "race")

    def test_a_contender_that_took_it_the_instant_the_holder_let_go_is_the_only_holder(
        self, opens
    ):
        """Had the holder deleted the file only AFTER letting go, this
        contender's check would pass on a file the delete then strands."""
        took = []
        with (
            contextlib.ExitStack() as holder,
            contextlib.ExitStack() as contender,
            contextlib.ExitStack() as third,
        ):
            assert _enter(holder, "race")
            opens.handles[0].after_close = lambda: took.append(
                _enter(contender, "race")
            )
            holder.close()
            assert took == [True], "nobody held it once the holder let go"
            assert not _enter(third, "race"), "a third taker got in beside it"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="an open file cannot be deleted on Windows: no lock lands on a nameless file",
    )
    def test_a_lock_let_go_under_every_attempt_is_refused_after_three(self, opens):
        """Each attempt's file is deleted between its open and its lock: a
        holder leaving every time. Three attempts, then LockHeld -- never a
        spin, never a lock on a file no path names."""
        path = lock_path("race")
        for n in range(10):
            opens.after[n] = path.unlink
        with contextlib.ExitStack() as taker:
            assert not _enter(taker, "race")
        assert len(opens.handles) == 3
        assert not path.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX order")
    def test_on_posix_the_holder_deletes_the_file_before_it_lets_go(self, opens):
        path = lock_path("race")
        seen = []
        with contextlib.ExitStack() as holder:
            assert _enter(holder, "race")
            opens.handles[0].after_close = lambda: seen.append(path.exists())
        assert seen == [False]

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows order")
    def test_on_windows_the_holder_still_lets_go_before_it_deletes(self, opens):
        """Unchanged on Windows: an open file cannot be deleted there, so the
        holder lets go first, deletes after, and the file is gone once out."""
        path = lock_path("race")
        seen = []
        with contextlib.ExitStack() as holder:
            assert _enter(holder, "race")
            opens.handles[0].after_close = lambda: seen.append(path.exists())
        assert seen == [True]
        assert not path.exists()


class TestPersistentLock:
    """The waiting lock: bounded by ``wait_s``, and its file is never deleted."""

    def test_the_file_outlives_the_holder(self):
        with persistent_lock("test", wait_s=1):
            assert lock_path("test").exists()
        assert lock_path("test").exists()
        assert lock_path("test") == Path.home() / ".magent" / "test.lock"

    def test_a_contender_gives_up_after_its_wait_with_lock_held(self):
        with persistent_lock("test", wait_s=1):
            with pytest.raises(LockHeld), persistent_lock("test", wait_s=0.1):
                pass
            assert lock_path("test").exists()

    def test_a_contender_gets_the_lock_once_the_holder_lets_go(self):
        order: list[str] = []
        held = threading.Event()

        def holder():
            with persistent_lock("test", wait_s=1):
                order.append("holder in")
                held.set()
                time.sleep(0.2)
                order.append("holder out")

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(timeout=5)
        with persistent_lock("test", wait_s=5):
            order.append("waiter in")
        t.join(timeout=5)
        assert order == ["holder in", "holder out", "waiter in"]

    def test_it_never_conflicts_with_a_different_name(self):
        with persistent_lock("alpha", wait_s=0), persistent_lock("beta", wait_s=0):
            pass
