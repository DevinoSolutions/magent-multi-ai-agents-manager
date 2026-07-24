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

from pathlib import Path


def _config_dir() -> Path:
    from magent.env import config_base  # heavy subsystem: in-body per policy

    return config_base() / "magent"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def find_config(config_arg: str | None) -> Path:
    if config_arg:
        return Path(config_arg)
    cwd = Path.cwd()
    for name in ("magent.config.json",):
        for loc in (cwd, cwd / "scripts"):
            if (loc / name).exists():
                return loc / name
    return _config_path()
