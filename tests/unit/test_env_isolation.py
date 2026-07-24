"""Seal (PR-B): conftest's autouse isolation must strip every ambient
process-env MAGENT_* var, not merely isolate the dotenv file.

On 2026-07-07 a dev machine's exported MAGENT_SENTRY_DSN reached the test
suite and leaked fake unit-test errors to prod Sentry (MAGENT-1..4). The
dotenv source was isolated afterwards, but get_env() still reads process env --
so an exported DSN would still initialize real Sentry inside a test run.

The module-scoped fixture below exports MAGENT_SENTRY_DSN into the REAL
os.environ the way a shell would. Because it is higher-scoped than conftest's
function-scoped autouse strip, it runs first -- the var is genuinely ambient
when the strip fires. Each test then proves the strip removed it; without the
conftest fix, all three fail (the DSN survives into get_env / init_sentry).
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from magent import cli, env

_EXPORTED_DSN = "https://exported@o0.ingest.sentry.io/424242"


@pytest.fixture(scope="module", autouse=True)
def _exported_sentry_dsn():
    """Simulate `export MAGENT_SENTRY_DSN=...` in the operator's shell."""
    previous = os.environ.get("MAGENT_SENTRY_DSN")
    os.environ["MAGENT_SENTRY_DSN"] = _EXPORTED_DSN
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MAGENT_SENTRY_DSN", None)
        else:
            os.environ["MAGENT_SENTRY_DSN"] = previous


def test_exported_dsn_is_stripped_from_process_env():
    assert "MAGENT_SENTRY_DSN" not in os.environ


def test_exported_dsn_never_reaches_get_env():
    # get_env() is what CLI entry reads to decide init_sentry; it sees no DSN.
    assert env.get_env().sentry_dsn is None


def test_cli_run_does_not_init_sentry_from_exported_dsn(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("magent.sentry.init_sentry", calls.append)
    result = CliRunner().invoke(cli.main, ["--config", "nope.json"])
    assert calls == []  # the exported DSN was stripped before app.py's gate
    assert result.exit_code == 1  # and the run reached the missing-config path
