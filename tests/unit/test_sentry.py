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
import json
import logging
import subprocess
import sys
import types

import pytest
from click.testing import CliRunner

from magent import cli, log
from magent.config import SCHEMA_VERSION
from magent.sentry import SENTRY_INSTALL_HINT, init_sentry
from tests.conftest import FakePlatform

_FAKE_DSN = "https://example@o0.ingest.sentry.io/0"


def _install_fake_sentry_sdk(
    monkeypatch: pytest.MonkeyPatch, version: str = "2.71.0"
) -> types.ModuleType:
    """Register a fake sentry_sdk package in sys.modules, recording every
    call so tests can assert on exactly what init_sentry passed it. ``version``
    picks the API shape: like the real SDK, a 3.x fake REJECTS the options 3.0
    removed instead of silently accepting them."""
    fake_sdk = types.ModuleType("sentry_sdk")
    fake_sdk.VERSION = version
    fake_integrations = types.ModuleType("sentry_sdk.integrations")
    fake_logging_mod = types.ModuleType("sentry_sdk.integrations.logging")
    fake_threading_mod = types.ModuleType("sentry_sdk.integrations.threading")

    fake_sdk.init_calls = []
    fake_sdk.flush_calls = []

    removed_in_3 = {"send_default_pii", "include_local_variables"}

    def _init(**kwargs: object) -> None:
        if version.startswith("3.") and removed_in_3 & kwargs.keys():
            raise TypeError(f"Unknown option {sorted(removed_in_3 & kwargs.keys())}")
        fake_sdk.init_calls.append(kwargs)

    def _flush(timeout: int) -> None:
        fake_sdk.flush_calls.append(timeout)

    fake_sdk.init = _init
    fake_sdk.flush = _flush

    class FakeLoggingIntegration:
        """The real signature per major: 3.0 added `breadcrumb_level` (default
        INFO -- so leaving it out means log breadcrumbs ARE on), and 2.x
        raises TypeError on it."""

        def __init__(
            self, level: int | None, event_level: int, **extra: object
        ) -> None:
            if version.startswith("3."):
                assert extra.keys() <= {"breadcrumb_level"}, extra
                self.breadcrumb_level = extra.get("breadcrumb_level", logging.INFO)
            elif extra:
                raise TypeError(f"unexpected keyword argument(s) {sorted(extra)}")
            self.level = level
            self.event_level = event_level

    class FakeThreadingIntegration:
        # Only the name both 2.x and 3.x accept: 3.0 removed `propagate_hub`.
        def __init__(self, propagate_scope: bool) -> None:
            self.propagate_scope = propagate_scope

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

        # sentry-sdk 2.x defaults this to True, and its scrubber redacts by key
        # NAME only: a local called `stdin`/`input_bytes` holding a node
        # payload would ship verbatim. The reproduction lives in
        # test_remote_mux.py::TestASecretNeverReachesSentry.
        assert call["include_local_variables"] is False

        assert len(registered) == 1, "init_sentry must register an atexit callback"
        registered[0]()  # invoke the registered callback directly
        assert fake_sdk.flush_calls == [2]

    def test_a_sentry_sdk_3_gets_data_collection_not_the_removed_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sentry-sdk 3.0 replaced send_default_pii/include_local_variables
        with `data_collection` and raises on the old names. The privacy
        guarantees (no PII, no frame locals) must survive the new spelling."""
        fake_sdk = _install_fake_sentry_sdk(monkeypatch, version="3.0.1")
        monkeypatch.setattr(atexit, "register", lambda _callback: None)

        init_sentry(_FAKE_DSN)  # a leftover removed option would raise here

        (call,) = fake_sdk.init_calls
        assert "send_default_pii" not in call
        assert "include_local_variables" not in call
        assert call["data_collection"]["user_info"] is False
        assert call["data_collection"]["stack_frame_variables"] is False
        (threading_integration,) = (
            i for i in call["integrations"] if hasattr(i, "propagate_scope")
        )
        assert threading_integration.propagate_scope is True

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

    def test_neither_major_gets_log_breadcrumbs_or_sentry_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2.x: `level` IS the breadcrumb level, so None switches them off.
        3.x: `level` is the Sentry-Logs level and `breadcrumb_level` defaults
        to INFO, so it has to be None too -- the real-SDK proof is
        test_sentry_real_sdk.py; this pins the spelling on both majors."""
        for version in ("2.71.0", "3.0.1"):
            fake_sdk = _install_fake_sentry_sdk(monkeypatch, version=version)
            monkeypatch.setattr(atexit, "register", lambda _callback: None)

            init_sentry(_FAKE_DSN)

            (call,) = fake_sdk.init_calls
            (integration,) = (i for i in call["integrations"] if hasattr(i, "level"))
            assert integration.level is None, version
            if version.startswith("3."):
                assert integration.breadcrumb_level is None
            assert call["max_breadcrumbs"] == 0, version

    def test_a_sentry_sdk_3_closes_every_data_collection_category(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3.x's `data_collection` has no off switch: a category left out of
        the options keeps its collecting default. Every category the 3.0 SDK
        defines is named, and off."""
        fake_sdk = _install_fake_sentry_sdk(monkeypatch, version="3.0.1")
        monkeypatch.setattr(atexit, "register", lambda _callback: None)

        init_sentry(_FAKE_DSN)

        (call,) = fake_sdk.init_calls
        closed = call["data_collection"]
        assert set(closed) == {
            "user_info",
            "cookies",
            "http_headers",
            "http_bodies",
            "url_query_params",
            "graphql",
            "gen_ai",
            "mcp",
            "database_query_data",
            "queues",
            "stack_frame_variables",
        }
        assert closed["user_info"] is False
        assert closed["stack_frame_variables"] is False
        assert closed["http_bodies"] == []
        assert closed["database_query_data"] is False
        assert closed["queues"] is False
        for key in ("cookies", "url_query_params"):
            assert closed[key] == {"mode": "off"}, key
        assert closed["http_headers"] == {"request": {"mode": "off"}}
        for key in ("graphql", "gen_ai", "mcp"):
            assert not any(closed[key].values()), key


class TestAnSdkThatRejectsTheOptionsNeverBreaksTheCommand:
    """init runs at CLI entry for every command: a future SDK renaming an
    option must turn reporting OFF, not raise out of `magent status`."""

    def test_an_init_typeerror_degrades_to_a_logged_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch, version="3.0.1")

        def _reject(**_kwargs: object) -> None:
            raise TypeError("Unknown option 'data_collection'")

        fake_sdk.init = _reject
        registered: list[object] = []
        monkeypatch.setattr(atexit, "register", registered.append)
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
        (record,) = records
        assert "reporting is OFF" in record
        assert "TypeError" in record
        # The type names the failure; the MESSAGE is the SDK's own text and
        # can carry the DSN (see TestTheInitFailureWarningNeverCarriesTheDsn),
        # so it is never logged.
        assert "Unknown option" not in record
        assert _FAKE_DSN not in record
        assert registered == [], "no flush hook for a client that never started"

    def test_a_rejected_integration_keyword_degrades_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The integrations are built inside the guard too: a future
        LoggingIntegration dropping `breadcrumb_level` raises from THEIR
        constructor, before init is even called."""
        # A "3.x" fake whose LoggingIntegration refuses the 3.x keyword.
        _install_fake_sentry_sdk(monkeypatch, version="3.0.1")
        logging_mod = sys.modules["sentry_sdk.integrations.logging"]

        class _Strict:
            def __init__(self, level: int | None, event_level: int) -> None:
                self.level, self.event_level = level, event_level

        monkeypatch.setattr(logging_mod, "LoggingIntegration", _Strict)
        monkeypatch.setattr(atexit, "register", lambda _callback: None)

        init_sentry(_FAKE_DSN)  # must not raise


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
            # A bare interpreter start has taken 14 s on a loaded machine.
            timeout=60,
            check=True,
        )
        assert result.stdout.strip() == "False"


# Unique markers in the three places a DSN carries identity: the public key,
# the host, and the project path segment (the SDK echoes the last one in
# "Invalid project in DSN ('...')"). A marker found anywhere in the log file is
# a leak.
_KEY_MARK = "KEYDECOY-3b9e51"
_HOST_MARK = "hostmark-3b9e51"
_PROJECT_MARK = "projmark-3b9e51"
_MARKED_DSN = f"https://{_KEY_MARK}@{_HOST_MARK}.invalid/{_PROJECT_MARK}"
_MARKS = (_KEY_MARK, _HOST_MARK, _PROJECT_MARK, _MARKED_DSN)


class TestTheInitFailureWarningNeverCarriesTheDsn:
    """sentry.log is a file people paste into bug reports, and an SDK
    exception's text is built from the DSN it was handed (BadDsn echoes the
    path segment, a port ValueError the port text). So the warning names the
    exception TYPE and the SDK version, never `str(exc)` and never a
    traceback (which carries the message). Asserted on the REAL log file, not
    on a stand-in logger, so an attached `exc_info` shows up too."""

    @pytest.fixture(autouse=True)
    def _fresh_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(atexit, "register", lambda _callback: None)
        log.reset_logging()
        yield
        log.reset_logging()

    @staticmethod
    def _sentry_log() -> str:
        path = log.LOG_DIR / "sentry.log"
        assert path.exists(), "the degraded init must leave a log line"
        return path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("version", ["2.71.0", "3.0.1"])
    def test_an_exception_message_carrying_the_dsn_stays_out_of_the_log(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        version: str,
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch, version=version)

        class BadDsn(ValueError):
            pass

        def _reject(**kwargs: object) -> None:
            dsn = str(kwargs["dsn"])
            # What the real SDK does with a bad DSN, and worse: the whole
            # string, in the message and in a chained cause.
            inner = ValueError(f"cannot parse {dsn}")
            raise BadDsn(f"Invalid project in DSN ({dsn!r})") from inner

        fake_sdk.init = _reject

        init_sentry(_MARKED_DSN)  # must not raise

        text = self._sentry_log()
        assert "BadDsn" in text
        assert version in text
        assert "reporting is OFF" in text
        assert "MAGENT_SENTRY_DSN" in text, "the fixed hint names what to check"
        for mark in _MARKS:
            assert mark not in text, mark
        assert "Traceback" not in text, "no exc_info on the warning"
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""

    def test_no_dsn_substring_survives_in_the_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Beyond the markers: no run of the DSN's identity (key, host or
        project, 6+ characters) appears in the line."""
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)

        def _reject(**kwargs: object) -> None:
            raise RuntimeError(str(kwargs["dsn"]))

        fake_sdk.init = _reject
        init_sentry(_MARKED_DSN)

        text = self._sentry_log()
        for part in (_KEY_MARK, _HOST_MARK, _PROJECT_MARK):
            for i in range(len(part) - 5):
                assert part[i : i + 6] not in text, part[i : i + 6]

    def test_the_missing_sdk_line_carries_no_dsn_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)

        init_sentry(_MARKED_DSN)

        text = self._sentry_log()
        assert "sentry-sdk is missing" in text
        for mark in _MARKS:
            assert mark not in text, mark


# --- doctor reads what init actually did -------------------------------------

# Appears only in the text of the exception the fake SDK raises, never in the
# DSN: proof that doctor does not echo `str(exc)` any more than the log does.
_MESSAGE_MARK = "SDK-MESSAGE-MARK-3b9e51"


class TestDoctorReadsTheInitOutcome:
    """`magent doctor` runs in the same process as the CLI-entry
    ``init_sentry`` that already tried the DSN, so the sentry check reads what
    that init recorded -- it does not guess from "a DSN is set and the SDK
    imports", and it never inits, flushes or registers anything itself. Each
    of the four states, in the plain checklist and in ``--json``."""

    @pytest.fixture(autouse=True)
    def _quiet_doctor(self, monkeypatch: pytest.MonkeyPatch):
        self.atexit_calls: list[object] = []
        monkeypatch.setattr(atexit, "register", self.atexit_calls.append)
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        log.reset_logging()
        yield
        log.reset_logging()

    @staticmethod
    def _doctor(tmp_config, dsn: str | None, *, as_json: bool):
        cfg = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        args = ["--config", cfg, "doctor"] + (["--json"] if as_json else [])
        env = {} if dsn is None else {"MAGENT_SENTRY_DSN": dsn}
        return CliRunner().invoke(cli.main, args, env=env)

    @staticmethod
    def _sentry_check(result, *, as_json: bool) -> tuple[str, str]:
        """(status, detail) of the sentry row, from whichever rendering."""
        if as_json:
            checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
            return checks["sentry"]["status"], checks["sentry"]["detail"]
        statuses = {"+": "ok", "!": "warn", "x": "fail"}
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] == "sentry" and parts[0] in statuses:
                return statuses[parts[0]], parts[2]
        raise AssertionError(f"no sentry row in:\n{result.stdout}")

    @pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
    def test_no_dsn_reads_off_and_ok(self, tmp_config, as_json: bool) -> None:
        result = self._doctor(tmp_config, None, as_json=as_json)
        status, detail = self._sentry_check(result, as_json=as_json)
        assert status == "ok"
        assert "off" in detail
        assert "MAGENT_SENTRY_DSN not set" in detail

    @pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
    def test_an_accepted_dsn_reads_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config, as_json: bool
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)
        result = self._doctor(tmp_config, _MARKED_DSN, as_json=as_json)
        status, detail = self._sentry_check(result, as_json=as_json)
        assert status == "ok"
        assert "active" in detail
        assert "2.71.0" in detail
        # init ran exactly once (CLI entry); doctor added nothing.
        assert len(fake_sdk.init_calls) == 1
        assert len(self.atexit_calls) == 1
        assert fake_sdk.flush_calls == []

    @pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
    def test_a_missing_sdk_warns_with_the_reinstall_hint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config, as_json: bool
    ) -> None:
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)
        result = self._doctor(tmp_config, _MARKED_DSN, as_json=as_json)
        status, detail = self._sentry_check(result, as_json=as_json)
        assert status == "warn"
        assert "sentry-sdk is missing" in detail
        assert "install looks broken" in detail
        assert SENTRY_INSTALL_HINT in detail

    @pytest.mark.parametrize("version", ["2.71.0", "3.0.1"])
    @pytest.mark.parametrize("as_json", [True, False], ids=["json", "plain"])
    def test_a_rejected_dsn_warns_naming_the_type_and_hint_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_config,
        as_json: bool,
        version: str,
    ) -> None:
        fake_sdk = _install_fake_sentry_sdk(monkeypatch, version=version)

        class BadDsn(ValueError):
            pass

        def _reject(**kwargs: object) -> None:
            raise BadDsn(f"{_MESSAGE_MARK} Invalid project in DSN ({kwargs['dsn']!r})")

        fake_sdk.init = _reject

        result = self._doctor(tmp_config, _MARKED_DSN, as_json=as_json)

        status, detail = self._sentry_check(result, as_json=as_json)
        assert status == "warn"
        assert "BadDsn" in detail
        assert version in detail
        assert "reporting is OFF" in detail
        assert "active" not in detail
        assert "MAGENT_SENTRY_DSN" in detail, "the repair hint names what to check"
        # Nothing of the DSN or of the exception's message, in any stream, nor
        # in the sentry log doctor sits beside.
        log_file = log.LOG_DIR / "sentry.log"
        streams = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "log": log_file.read_text(encoding="utf-8") if log_file.exists() else "",
        }
        for name, text in streams.items():
            for mark in (*_MARKS, _MESSAGE_MARK):
                assert mark not in text, (name, mark)
        assert "Traceback" not in result.stdout + result.stderr
        # Doctor re-initialised nothing and registered no second flush.
        assert self.atexit_calls == []

    def test_a_rejected_dsn_is_a_warning_not_a_failed_doctor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config
    ) -> None:
        """WARN-at-worst, like its siblings: the check never decides the exit
        code."""
        fake_sdk = _install_fake_sentry_sdk(monkeypatch)

        def _reject(**kwargs: object) -> None:
            raise ValueError("no")

        fake_sdk.init = _reject
        result = self._doctor(tmp_config, _MARKED_DSN, as_json=True)
        report = json.loads(result.stdout)
        sentry_row = next(c for c in report["checks"] if c["name"] == "sentry")
        assert sentry_row["status"] == "warn"
        assert report["failures"] == sum(
            1 for c in report["checks"] if c["status"] == "fail"
        )
