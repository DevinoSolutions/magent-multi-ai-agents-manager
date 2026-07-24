"""Validated environment schema — the single module that touches process env.

App-config variables (MAGENT_*) are validated via pydantic-settings:
unknown MAGENT_ vars = hard error (closed schema). Host-infrastructure
variables (APPDATA, XDG_CONFIG_HOME, LOCALAPPDATA, EDITOR) are defaulted
reads — not validated app config; the two concerns are visibly separate.

The optional dotenv file lives at ``~/.magent/.env`` (ENV_FILE), next to
the logs and agent-state dirs — NEVER the current directory's ``.env``.
magent is a launcher run from arbitrary project directories, and with
``extra="forbid"`` a CWD read hard-fails startup on any foreign project's
perfectly innocent ``.env`` keys.

Env errors are pre-Sentry by construction: the DSN comes from env, so
a bad env can't be reported to Sentry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import (
    HttpUrl,  # reason: pydantic needs HttpUrl at runtime for model validation
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings

# magent's own dotenv file — module attribute (not baked into model_config)
# so tests can monkeypatch it and get_env() reads the patched value at call
# time. Bare MagentEnv() reads process env only.
ENV_FILE = Path.home() / ".magent" / ".env"


class MagentEnv(BaseSettings):
    """Validated MAGENT_* environment variables.

    ``extra="forbid"`` means any unknown ``MAGENT_*`` var is a hard error
    (closed schema — same doctrine as the config file). pydantic-settings only
    ever reads env keys that map to a declared field, so ``extra="forbid"``
    alone never sees the rest; the ``_no_unknown_magent_vars`` validator
    below closes that hole by scanning ``os.environ`` directly. Dotenv keys
    are different: pydantic-settings loads the whole file (prefixed or not),
    so ``extra="forbid"`` rejects every unknown entry in ENV_FILE — which is
    correct there, because that file belongs to magent alone.
    """

    model_config = {"env_prefix": "MAGENT_", "extra": "forbid"}

    sentry_dsn: HttpUrl | None = None
    ntfy_topic: HttpUrl | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None

    @model_validator(mode="after")
    def _no_unknown_magent_vars(self) -> MagentEnv:
        """Hard-fail on any MAGENT_* env var this schema doesn't declare."""
        known = {f"MAGENT_{name.upper()}" for name in type(self).model_fields}
        unknown = sorted(
            key.upper()
            for key in os.environ
            if key.upper().startswith("MAGENT_") and key.upper() not in known
        )
        if unknown:
            raise ValueError(
                "Unknown MAGENT_* environment variable(s): " + ", ".join(unknown)
            )
        return self


_cached_env: MagentEnv | None = None


def get_env() -> MagentEnv:
    """Return the validated env singleton (instantiated on first call)."""
    global _cached_env  # noqa: PLW0603  # reason: module-level cache singleton pattern
    if _cached_env is None:
        _cached_env = MagentEnv(_env_file=ENV_FILE)  # ty: ignore[unknown-argument]  # reason: _env_file is pydantic-settings' documented per-call dotenv override; ty can't see BaseSettings' synthesized __init__
    return _cached_env


def validation_error_items(exc: ValidationError) -> list[tuple[str, str]]:
    """(display-name, message) pairs for a MagentEnv ValidationError.

    Field errors carry a bare field name in ``loc`` and always map to a
    ``MAGENT_*`` variable, so the prefix is prepended. ``extra_forbidden``
    errors carry the raw key from ENV_FILE verbatim — any name at all — and
    must be shown as-is: rebranding ``EBAY_TOKEN`` as ``MAGENT_EBAY_TOKEN``
    sends the reader hunting for a variable that exists nowhere. The
    ``_no_unknown_magent_vars`` errors have an empty ``loc`` (name ``""``);
    their message already lists the full variable names.
    """
    items: list[tuple[str, str]] = []
    for error in exc.errors():
        name = ".".join(str(part) for part in error["loc"]).upper()
        if (
            name
            and error["type"] != "extra_forbidden"
            and not name.startswith("MAGENT_")
        ):
            name = f"MAGENT_{name}"
        items.append((name, error["msg"]))
    return items


# --- Host-infrastructure env reads -----------------------------------------
# These are NOT app-config: they are OS-convention variables with sensible
# defaults. Grouped here so TID251 can ban os.environ everywhere else.


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def localappdata_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ""))


def editor_command() -> str:
    return os.environ.get("EDITOR", "xdg-open")


def config_base() -> Path:
    """The platform-appropriate config base directory."""
    if sys.platform == "win32":
        return appdata_dir()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return xdg_config_home()


def vscode_storage_base() -> Path:
    """The platform-appropriate VS Code workspace storage parent."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", ""))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
