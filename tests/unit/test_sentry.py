"""Sentry capturability contract (R2-03/R2-04): errors-only + no-PII init
config, ERROR-level logging integration, and a console-quiet (but logged)
missing-sdk path.

Every test injects a FAKE sentry_sdk into sys.modules mirroring the exact
import shape init_sentry uses (sentry_sdk, .integrations.logging,
.integrations.threading), so these never touch the real bundled sentry-sdk
and pass even in an environment where it is absent.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import sys
import types
from typing import TYPE_CHECKING

from magent.sentry import SENTRY_INSTALL_HINT, init_sentry

if TYPE_CHECKING:
    import pytest

_FAKE_DSN = "https://example@o0.ingest.sentry.io/0"


def _install_fake_sentry_sdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Register a fake sentry_sdk package in sys.modules, recording every
    call so tests can assert on exactly what init_sentry passed it."""
    fake_sdk = types.ModuleType("sentry_sdk")
    fake_integrations = types.ModuleType("sentry_sdk.integrations")
    fake_logging_mod = types.ModuleType("sentry_sdk.integrations.logging")
    fake_threading_mod = types.ModuleType("sentry_sdk.integrations.threading")

    fake_sdk.init_calls = []
    fake_sdk.flush_calls = []

    def _init(**kwargs: object) -> None:
        fake_sdk.init_calls.append(kwargs)

    def _flush(timeout: int) -> None:
        fake_sdk.flush_calls.append(timeout)

    fake_sdk.init = _init
    fake_sdk.flush = _flush

    class FakeLoggingIntegration:
        def __init__(self, level: int | None, event_level: int) -> None:
            self.level = level
            self.event_level = event_level

    class FakeThreadingIntegration:
        def __init__(self, propagate_hub: bool) -> None:
            self.propagate_hub = propagate_hub

    fake_logging_mod.LoggingIntegration = FakeLoggingIntegration
    fake_threading_mod.ThreadingIntegration = FakeThreadingIntegration
    fake_integrations.logging = fake_logging_mod
    fake_integrations.threading = fake_threading_mod
    fake_sdk.integrations = fake_integrations

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", fake_integrations)
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.logging", fake_logging_mod
    )
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.threading", fake_threading_mod
    )
    return fake_sdk


class TestCapturabilityContract:
    """R2-03: init_sentry must configure errors-only, no-PII capture and
    register an atexit flush -- proven against a fake transport, not memory."""

    def test_init_is_errors_only_no_pii_with_atexit_flush(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)
        registered: list[object] = []
        monkeypatch.setattr(atexit, "register", registered.append)

        init_sentry(_FAKE_DSN)

        assert len(fake_sdk.init_calls) == 1
        call = fake_sdk.init_calls[0]
        assert call["dsn"] == _FAKE_DSN
        assert call["traces_sample_rate"] == 0
        assert call["send_default_pii"] is False

        assert len(registered) == 1, "init_sentry must register an atexit callback"
        registered[0]()  # invoke the registered callback directly
        assert fake_sdk.flush_calls == [2]

    def test_init_tags_environment_dev_and_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live-verification by-product (MAGENT-5/6): the untagged init
        defaulted environment to 'production' on a dev box and pinned release
        to git HEAD only by accident of running inside the repo. Pin both: a
        fixed 'dev' environment and a release key derived from the installed
        version (present even if the distribution lookup returns None)."""
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)
        monkeypatch.setattr(atexit, "register", lambda _callback: None)

        init_sentry(_FAKE_DSN)

        call = fake_sdk.init_calls[0]
        assert call["environment"] == "dev"
        assert "release" in call  # tag always present; value is version or None

    def test_logging_integration_is_error_level_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)
        monkeypatch.setattr(atexit, "register", lambda _callback: None)

        init_sentry(_FAKE_DSN)

        integrations = fake_sdk.init_calls[0]["integrations"]
        logging_integration = next(i for i in integrations if hasattr(i, "event_level"))
        assert logging_integration.event_level == logging.ERROR


class TestMissingSdkIsQuietButLogged:
    """R2-04 (revised twice): sentry-sdk is a base dependency now, so a
    missing SDK means a broken install -- but even then init must NEVER nag
    the console: it runs at CLI entry for every command, so a stderr line
    would tax unrelated commands like `attach` on every run. The explanation
    still exists (never silent-with-zero-trace): a rotating-log warning here,
    plus the repair hint in `magent doctor`. init still must not raise."""

    def test_missing_sdk_logs_warning_console_stays_clean(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)
        records: list[str] = []

        class _RecordingLogger:
            def warning(self, msg: str, *args: object) -> None:
                records.append(msg % args if args else msg)

        monkeypatch.setattr(
            "magent.sentry.get_logger", lambda _name: _RecordingLogger()
        )

        init_sentry(_FAKE_DSN)  # must not raise

        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
        assert len(records) == 1
        assert "sentry-sdk is missing" in records[0]
        assert SENTRY_INSTALL_HINT in records[0]


class TestNoEagerImport:
    """Pin: merely importing magent.sentry must never pull in sentry_sdk
    -- the SDK is bundled but loads only inside init_sentry's guarded try,
    keeping `--help`-style startup free of its import cost when no DSN is
    set. Runs in a fresh subprocess so no other test's sys.modules state can
    hide a regression here."""

    def test_importing_the_module_does_not_import_sentry_sdk(self) -> None:
        probe = "import sys; import magent.sentry; print('sentry_sdk' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout.strip() == "False"
