"""Config-editor JSON I/O: the exit-on-error face of ``magent.config_io``.

The round-tripping raw-dict path (preserves unknown/unmodeled keys on every
read-modify-write) lives in ``src/magent/config_io.py`` so ``projects.py`` and
the API can use it; this module keeps the editor's exit-1 style and the
raw-dict narrowing helpers. Deliberately separate from
magent.config.load_config (the validated typed path for runtime consumption):
round-tripping an editor save through the dataclass would silently lose data
(E6.md S2.4 / S0 deviation).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import click

from magent import config_io
from magent.config import load_config
from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

    from magent.config import MagentConfig


def _load_raw_config(path: Path) -> dict[str, object]:
    if not path.exists():
        click.echo(f"No config found at: {path}", err=True)
        click.echo(f"Run {style('magent', bold=True)} to generate one.", err=True)
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Config at {path} is not a JSON object")
    return data


def _save_raw_config(path: Path, data: dict[str, object]) -> None:
    """The interactive editor's save: ``config_io.save`` without validation
    (the editor may pass through a state the user is still fixing), but with
    the backup and the atomic write every config write now gets."""
    config_io.save(path, data, validate=False)


def _save_raw_config_atomic(path: Path, data: dict[str, object]) -> None:
    """Write DATA over PATH without ever leaving a half-written config -- the
    whole config arriving over SSH (``config put``) or from node onboarding,
    already validated by the caller. Same write as ``_save_raw_config``; the
    name stays for its callers. Known overlap: ``config put`` also keeps its
    own ``<config>.bak-remote-edit`` copy, so a remote edit is backed up twice
    (there and in ``~/.magent/backups``)."""
    config_io.save(path, data, validate=False)


def _validate_config_text(text: str) -> str | None:
    """Delegate to ``config_io.validate_text``: ``None`` when TEXT would load
    as a valid config, else the reason it would not."""
    return config_io.validate_text(text)


# --- Raw-dict narrowing helpers ------------------------------------------
# The editor path round-trips arbitrary JSON (see the module docstring), so
# every nested value arrives typed as ``object`` and must be narrowed at each
# use site. These keep that narrowing in one place instead of scattering
# isinstance checks through the editor. ``_sub``/``_sublist`` additionally
# insert-and-return, so a caller can mutate the result and have it persist
# (setdefault semantics) -- the read-modify-write the interactive editor needs.


def _as_dict(value: object) -> dict[str, object]:
    """View an unknown config value as a string-keyed dict (empty if it is not
    one). Returns the value itself when it is a dict, so in-place edits persist."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """View an unknown config value as a list (empty if it is not one)."""
    return value if isinstance(value, list) else []


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_int(value: object, default: int) -> int:
    # bool is an int subclass; a JSON ``true`` must not read back as 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _project_dicts(data: dict[str, object]) -> list[dict[str, object]]:
    """The config's project entries, keeping only well-formed dict entries.
    Element identity is preserved, so mutating an entry in place persists."""
    return [p for p in _as_list(data.get("projects")) if isinstance(p, dict)]


def _sub(d: dict[str, object], key: str) -> dict[str, object]:
    """``d[key]`` as a dict, inserting a fresh one when absent or not a dict.
    The returned dict is stored back in ``d`` (setdefault semantics), so
    mutations to it persist."""
    value = d.get(key)
    if isinstance(value, dict):
        return value
    fresh: dict[str, object] = {}
    d[key] = fresh
    return fresh


def _sublist(d: dict[str, object], key: str) -> list[object]:
    """``d[key]`` as a list, inserting a fresh one when absent or not a list.
    The returned list is stored back in ``d``, so append/pop persist."""
    value = d.get(key)
    if isinstance(value, list):
        return value
    fresh: list[object] = []
    d[key] = fresh
    return fresh


def _load_config_or_exit(config_file: Path, *, as_json: bool = False) -> MagentConfig:
    """Load the typed config or exit 1. ``as_json`` emits a machine-readable
    ``{"ok": false, "error": ...}`` envelope on stdout instead of a plain-text
    ``Error:`` line on stderr -- so a ``--json`` caller (status/up) always gets
    JSON on the config-error path, never a stderr diagnostic (NF-S3-005)."""
    try:
        return load_config(str(config_file))
    except (
        ValueError,
        FileNotFoundError,
    ) as e:  # ConfigError <: ValueError (E7 S2d) -> caught
        if as_json:
            click.echo(json.dumps({"ok": False, "error": str(e)}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(1)
