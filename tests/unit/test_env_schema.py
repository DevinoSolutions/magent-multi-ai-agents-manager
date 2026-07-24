"""Drift-pin: .env.example keys ⇔ MagentEnv fields.

Same pattern as test_config_factory.py::TestExampleConfigMatchesFactory.
A new env var without an .env.example update = red gate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from magent import cli
from magent import env as env_module
from magent.env import MagentEnv

_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _ROOT / ".env.example"


def _clear_magent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ambient MAGENT_* process var so tests start from a
    known-empty schema (conftest already isolates ENV_FILE to tmp_path)."""
    for key in list(os.environ):
        if key.upper().startswith("MAGENT_"):
            monkeypatch.delenv(key, raising=False)


def _example_keys() -> set[str]:
    """Parse commented-out MAGENT_* keys from .env.example."""
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"#?\s*(MAGENT_\w+)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def _schema_keys() -> set[str]:
    """MAGENT_* env-var names implied by MagentEnv fields."""
    prefix = "MAGENT_"
    return {f"{prefix}{name.upper()}" for name in MagentEnv.model_fields}


class TestEnvExampleMatchesSchema:
    def test_example_keys_match_schema(self) -> None:
        example = _example_keys()
        schema = _schema_keys()
        assert example == schema, (
            f"Drift detected between .env.example and MagentEnv.\n"
            f"  In example but not schema: {example - schema}\n"
            f"  In schema but not example: {schema - example}"
        )

    def test_example_file_exists(self) -> None:
        assert _ENV_EXAMPLE.is_file(), ".env.example must be committed"

    @pytest.mark.parametrize("key", sorted(_schema_keys()))
    def test_each_schema_key_in_example(self, key: str) -> None:
        assert key in _example_keys(), f"{key} missing from .env.example"


class TestClosedSchemaRejectsUnknownVars:
    """R2-01: extra="forbid" alone never sees env-sourced keys — the
    _no_unknown_magent_vars validator is what actually closes the schema."""

    def test_unknown_var_is_a_hard_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_magent_env(monkeypatch)
        monkeypatch.setenv("MAGENT_TOTALLY_UNKNOWN", "1")
        with pytest.raises(ValidationError):
            MagentEnv(_env_file=None)

    def test_known_vars_still_validate_fine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_magent_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "DEBUG")
        env = MagentEnv(_env_file=None)
        assert env.log_level == "DEBUG"


class TestCliFailsFastOnBadEnv:
    """R2-02: a malformed/unknown MAGENT_* var must exit 1 with a plain
    stderr message naming the variable — never a raw pydantic traceback."""

    def test_bad_log_level_exits_nonzero_naming_the_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_magent_env(monkeypatch)
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "BOGUS")
        monkeypatch.setattr(env_module, "_cached_env", None)

        result = CliRunner().invoke(cli.main, [])

        assert result.exit_code == 1
        assert "MAGENT_LOG_LEVEL" in result.output
        assert "Traceback" not in result.output

    def test_unknown_var_exits_nonzero_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_magent_env(monkeypatch)
        monkeypatch.setenv("MAGENT_TOTALLY_UNKNOWN", "1")
        monkeypatch.setattr(env_module, "_cached_env", None)

        result = CliRunner().invoke(cli.main, [])

        assert result.exit_code == 1
        assert "MAGENT_TOTALLY_UNKNOWN" in result.output
        assert "Traceback" not in result.output

    def test_unknown_var_in_own_env_file_names_it_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R5-01: an ENV_FILE extra_forbidden error carries the full
        MAGENT_* key in loc (unlike field errors, which carry the bare
        field name) — the formatter must not double the prefix."""
        _clear_magent_env(monkeypatch)
        env_module.ENV_FILE.write_text("MAGENT_FOO=1\n", encoding="utf-8")

        result = CliRunner().invoke(cli.main, [])

        assert result.exit_code == 1
        assert "MAGENT_FOO: " in result.output
        assert "MAGENT_MAGENT" not in result.output
        assert "Traceback" not in result.output


class TestEnvFileIsMagentsOwn:
    """Field incident (2026-07-07): magent is a launcher run from arbitrary
    project directories. It must read only its own ~/.magent/.env
    (env.ENV_FILE) — a CWD .env belongs to whatever project lives there, and
    with extra="forbid" its innocent keys (eBay tokens, in the original
    report) hard-failed magent's startup under a fabricated MAGENT_
    name."""

    def test_foreign_dotenv_in_cwd_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_magent_env(monkeypatch)

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path(".env").write_text(
                "EBAY_APP_ACCESS_TOKEN=x\nSUPER=y\n", encoding="utf-8"
            )
            # --config to a missing path: proves the run got PAST env
            # validation (config resolution errors, not the env layer) and
            # keeps the test off any real config on the host machine.
            result = runner.invoke(cli.main, ["--config", "nope.json"])

        assert "Extra inputs are not permitted" not in result.output
        assert "EBAY_APP_ACCESS_TOKEN" not in result.output
        assert "No config found" in result.output
        assert "Traceback" not in result.output

    def test_extra_key_in_own_env_file_names_raw_key_and_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown entry in ENV_FILE errors under its raw name — no
        fabricated MAGENT_ prefix — and the hint names the file."""
        _clear_magent_env(monkeypatch)
        env_module.ENV_FILE.write_text("EBAY_APP_ACCESS_TOKEN=x\n", encoding="utf-8")

        result = CliRunner().invoke(cli.main, [])

        assert result.exit_code == 1
        assert "EBAY_APP_ACCESS_TOKEN: " in result.output
        assert "MAGENT_EBAY_APP_ACCESS_TOKEN" not in result.output
        assert str(env_module.ENV_FILE) in result.output

    def test_known_var_in_own_env_file_is_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_magent_env(monkeypatch)
        env_module.ENV_FILE.write_text("MAGENT_LOG_LEVEL=DEBUG\n", encoding="utf-8")

        assert env_module.get_env().log_level == "DEBUG"
