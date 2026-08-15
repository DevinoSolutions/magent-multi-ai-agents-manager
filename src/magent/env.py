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
    # Whether `magent serve` keeps the Alt+V listener alive (see
    # upload_server._supervise_hotkey). On by default -- a serve daemon with no
    # listener means Alt+V silently does nothing, which is the failure this
    # supervision exists to end.
    #
    # The opt-out is not decoration. The listener installs a SYSTEM-WIDE
    # low-level keyboard hook, so any test that starts a real `magent serve` on
    # Windows would otherwise install one on the developer's own desktop -- the
    # e2e/soak/dist serve fixtures set this to 0 for exactly that reason (the
    # same posture as the opt-in `MDTEST_INTERACTION` tier). It doubles as the
    # escape hatch for a user who wants to own the listener's lifetime.
    hotkey_supervisor: bool = True

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


# The tmux-side "you are inside a session" markers, by exact name: tmux sets
# TMUX (socket,pid,session of the CLIENT) and TMUX_PANE, and those two alone are
# what its nested-session guard reads. NOT a prefix match -- see _MUX_KEEP_SUFFIX.
_MUX_NESTING_VARS = frozenset({"TMUX", "TMUX_PANE"})

# psmux's own markers (PSMUX_SESSION, PSMUX_TARGET_SESSION,
# PSMUX_CLAUDE_TEAMMATE_MODE, ...) ARE prefix-matched: that set is psmux's to
# grow, not ours, and every member so far is a session marker.
_MUX_NESTING_PREFIX = "PSMUX"

# ...with one carve-out, in both families. A ``*_TMPDIR`` variable is
# CONFIGURATION, not a nesting marker: TMUX_TMPDIR names the directory the
# server's sockets live in, so a child that loses it looks for the server in a
# different place and finds nothing at all. Every session then probes DEAD --
# the exact failure the browser-upload CI tier hit (it confines its real-tmux
# shim to a private TMUX_TMPDIR, so stripping it emptied /api/sessions), and the
# same would happen to any user who relocates their sockets.
_MUX_KEEP_SUFFIX = "_TMPDIR"


def _is_mux_nesting_marker(key: str) -> bool:
    """True for the env vars that say "this process is INSIDE a session"."""
    upper = key.upper()
    if upper.endswith(_MUX_KEEP_SUFFIX):
        return False
    return upper in _MUX_NESTING_VARS or upper.startswith(_MUX_NESTING_PREFIX)


def psmux_child_env() -> dict[str, str]:
    """The process environment with the psmux/tmux nesting markers removed.

    magent's sessions are SIBLINGS by construction -- one session per socket,
    never a session inside a session -- but a magent command run from inside a
    magent psmux window inherits that window's ``PSMUX_SESSION``/``TMUX``, and
    the psmux binary then refuses the child with::

        psmux: sessions should be nested with care, unset PSMUX_SESSION to force

    (and, on psmux 3.3.6, still exits 0 while creating nothing). Stripping the
    markers is what makes a bring-up launched from inside a session behave
    exactly like one launched from a bare shell.

    Removing ``TMUX``/``TMUX_PANE`` also removes the ambient default target:
    with them unset, no control command can silently answer for "the calling
    client's own session" -- the same class of bug ``pane_cwd``'s explicit
    ``-t`` exists to close.

    What must SURVIVE, and why: ``TMUX_TMPDIR`` (and any future ``*_TMPDIR``)
    tells the binary WHERE the server sockets live -- it is how you reach a
    server, not a claim to be running inside one. A child without it falls back
    to the default socket dir, finds no server there, and reports every session
    dead. Load-bearing for anyone who relocates their sockets, and proven by
    CI: the browser-upload tier confines its real-tmux shim to a private
    ``TMUX_TMPDIR``, and a blanket ``TMUX*`` strip made the upload page render
    an empty project list.

    Used for CREATION/CONTROL/PROBE children only. A user-facing ``attach``
    client is a different question (attaching from inside a pane really IS
    nesting, and psmux's guard is right to fire there), so those call sites
    keep the inherited environment on purpose.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not _is_mux_nesting_marker(key)
    }


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
