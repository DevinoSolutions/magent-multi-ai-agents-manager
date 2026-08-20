"""Unit tests for magent.log -- rotating file logging + liveness heartbeats.

Cross-platform (stdlib only) -- must run clean on the Linux/macOS/Windows CI
legs, not just Windows.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from magent import env, log


class TestGetLogger:
    def test_idempotent_same_instance(self):
        a = log.get_logger("upload")
        b = log.get_logger("upload")
        assert a is b
        assert len(a.handlers) == 1  # repeat calls never stack handlers

    def test_handler_is_rotating_file_handler_with_expected_config(self):
        logger = log.get_logger("upload")
        handler = logger.handlers[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 1_000_000
        assert handler.backupCount == 3

    def test_logged_line_lands_in_named_log_file(self):
        logger = log.get_logger("upload")
        logger.info("hello from test")
        log_file = log.LOG_DIR / "upload.log"
        assert log_file.exists()
        assert "hello from test" in log_file.read_text(encoding="utf-8")

    def test_mkdir_failure_falls_back_to_null_handler(self, monkeypatch):
        def _raise(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", _raise)

        logger = log.get_logger("upload")
        assert isinstance(logger.handlers[0], logging.NullHandler)
        logger.info("must not raise")  # best-effort: still usable


class TestSharedRotatingHandler:
    """The handler several magent processes point at the same file.

    The end-to-end proof is ``tests/e2e/test_log_multiprocess.py`` (real
    processes, real contention). These are the cheap, all-OS pins on the
    properties that make it work -- and on the one thing consumers can see.
    """

    def test_no_file_is_held_open_between_records(self):
        """The invariant the whole design rests on: nothing holds the log
        across records, so another process's rollover rename can never be
        blocked (WinError 32) and no two writers can resolve the same offset."""
        logger = log.get_logger("shared")
        handler = logger.handlers[0]
        logger.info("first")
        assert handler.stream is None
        logger.info("second")
        assert handler.stream is None
        text = (log.LOG_DIR / "shared.log").read_text(encoding="utf-8")
        assert "first" in text
        assert "second" in text

    def test_lock_sidecar_is_not_mistaken_for_a_rotated_log(self):
        """Consumers enumerate rotated files with ``glob("<name>.log*")`` --
        tests/e2e/test_soak.py::_assert_logs_bounded is one, a shell is
        another. The interlock's sidecar must sit outside that glob."""
        log.get_logger("shared").info("touch")
        assert (log.LOG_DIR / "shared.lock").exists()
        assert sorted(p.name for p in log.LOG_DIR.glob("shared.log*")) == ["shared.log"]

    def test_rotation_still_happens_and_stays_bounded(self, monkeypatch):
        monkeypatch.setattr(log, "_MAX_BYTES", 2_000)
        monkeypatch.setattr(log, "_BACKUP_COUNT", 2)
        log.reset_logging()
        logger = log.get_logger("shared")
        for i in range(200):
            logger.info("record %03d %s", i, "z" * 60)

        rotated = sorted(p.name for p in log.LOG_DIR.glob("shared.log*"))
        assert rotated == ["shared.log", "shared.log.1", "shared.log.2"]
        for name in rotated:
            size = (log.LOG_DIR / name).stat().st_size
            # maxBytes plus at most the one record that crossed the threshold.
            assert size <= 2_000 + 512, f"{name} is {size} bytes"

    def test_a_faked_sys_platform_cannot_break_logging(self, monkeypatch):
        """Tests monkeypatch ``sys.platform`` to drive the win32 branches of
        platform-specific code (`test_upload_server.py`'s taskkill path is one),
        and that code logs. A logger that re-read ``sys.platform`` per record
        would try to `import msvcrt` on Linux and take down the very call it
        exists to observe -- which is exactly what happened on this change's
        first CI run. The locking primitive is bound once at import instead."""
        logger = log.get_logger("shared")
        # Resolved BEFORE the fakes: monkeypatch.undo() would also revert
        # conftest's LOG_DIR redirect and send this read at the real ~/.magent.
        log_file = log.LOG_DIR / "shared.log"
        for faked in ("win32", "linux", "darwin"):
            monkeypatch.setattr(sys, "platform", faked)
            logger.info("logged while sys.platform said %s", faked)

        text = log_file.read_text(encoding="utf-8")
        for faked in ("win32", "linux", "darwin"):
            assert f"sys.platform said {faked}" in text

    def test_close_releases_the_lock_file(self):
        """A leaked lock descriptor would make the sidecar undeletable on
        Windows -- and would outlive reset_logging() in the test suite."""
        log.get_logger("shared").info("touch")
        lock = log.LOG_DIR / "shared.lock"
        log.reset_logging()
        lock.unlink()  # PermissionError on Windows if the fd is still open
        assert not lock.exists()

    def test_unobtainable_lock_still_writes_the_record_and_says_so(self, monkeypatch):
        """Degrading to an unlocked write keeps the record -- losing it is the
        defect this handler exists to remove -- but must never be silent."""
        logger = log.get_logger("shared")
        handler = logger.handlers[0]
        monkeypatch.setattr(handler, "_acquire", lambda: False)

        logger.info("degraded one")
        logger.info("degraded two")

        text = (log.LOG_DIR / "shared.log").read_text(encoding="utf-8")
        assert "degraded one" in text
        assert "degraded two" in text
        assert text.count("log interlock unavailable") == 1  # once per process
        assert "WARNING" in text


class TestLogLevelFromEnv:
    """P2-01: get_logger honors MAGENT_LOG_LEVEL. It was validated in the
    env schema and documented in .env.example but never applied -- log.py
    hardcoded INFO, so the knob you'd reach for to debug a daemon did nothing."""

    def test_honors_debug_from_env(self, monkeypatch):
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "DEBUG")
        monkeypatch.setattr(env, "_cached_env", None)
        log.reset_logging()
        assert log.get_logger("leveltest").level == logging.DEBUG

    def test_defaults_to_info_when_unset(self, monkeypatch):
        # conftest's autouse strip already removed any ambient MAGENT_*.
        monkeypatch.setattr(env, "_cached_env", None)
        log.reset_logging()
        assert log.get_logger("leveltest").level == logging.INFO

    def test_bad_value_falls_back_to_info_not_crash(self, monkeypatch):
        # A bad value fails fast at CLI entry; if get_logger is still reached
        # (a detached daemon whose inherited env went bad), it must default to
        # INFO rather than crash the process it exists to observe.
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "BOGUS")
        monkeypatch.setattr(env, "_cached_env", None)
        log.reset_logging()
        assert log.get_logger("leveltest").level == logging.INFO  # must not raise


class TestHeartbeat:
    def test_write_then_fresh(self):
        log.write_heartbeat("hotkey")
        age = log.heartbeat_age("hotkey")
        assert age is not None
        assert age < 1.0
        assert log.heartbeat_fresh("hotkey") is True

    def test_missing_heartbeat_is_none_and_not_fresh(self):
        assert log.heartbeat_age("nonexistent") is None
        assert log.heartbeat_fresh("nonexistent") is False

    def test_stale_heartbeat_is_not_fresh(self):
        log.write_heartbeat("hotkey")
        path = log._heartbeat_path("hotkey")
        old = time.time() - 120
        os.utime(path, (old, old))
        assert log.heartbeat_fresh("hotkey", max_age=30) is False
