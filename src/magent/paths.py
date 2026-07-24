"""Config file location -- a pure leaf with no dependency on the cli package
or any other magent subsystem (stdlib only). Imported by both
magent.cli and magent.upload_server; living at the TOP level (not
cli/paths.py) is what lets upload_server depend on it without importing the
cli *package* -- LS-A-001: that is the real, structural fix for the latent
load cycle (upload_server's command modules are imported by cli/__init__ for
registration; if the config-path leaf lived inside the cli package,
upload_server would depend back on the package it's imported by).
"""

from __future__ import annotations

import sys
from pathlib import Path

# multideck's pre-rename config directory. Kept only as a read-only fallback so
# an existing install keeps finding its config after the rename; multideck never
# writes here and never migrates it — the user renames the directory themselves.
_LEGACY_DIR_NAME = "multideck"

_legacy_warned = False


def _config_dir() -> Path:
    from magent.env import config_base  # heavy subsystem: in-body per policy

    return config_base() / "magent"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _legacy_config_path() -> Path:
    from magent.env import config_base  # heavy subsystem: in-body per policy

    return config_base() / _LEGACY_DIR_NAME / "config.json"


def _warn_legacy_config(path: Path) -> None:
    global _legacy_warned  # noqa: PLW0603  # reason: module-level one-time-warning latch
    if _legacy_warned:
        return
    _legacy_warned = True
    sys.stderr.write(
        f"magent: using legacy config at {path}. multideck was renamed to magent; "
        f"move it to {_config_path()} (or rename the '{_LEGACY_DIR_NAME}' config "
        "directory to 'magent') to silence this warning.\n"
    )


def find_config(config_arg: str | None) -> Path:
    if config_arg:
        return Path(config_arg)
    cwd = Path.cwd()
    for name in ("magent.config.json",):
        for loc in (cwd, cwd / "scripts"):
            if (loc / name).exists():
                return loc / name
    primary = _config_path()
    if not primary.exists():
        legacy = _legacy_config_path()
        if legacy.exists():
            _warn_legacy_config(legacy)
            return legacy
    return primary
