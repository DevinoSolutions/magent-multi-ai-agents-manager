"""Tests for the exclusive lockfile used by daemon startup guards."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from magent.lockfile import LockHeld, exclusive_lock


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


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
