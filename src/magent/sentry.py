"""Sentry error reporting — env-gated, default OFF; SDK bundled.

sentry-sdk is a base dependency (see pyproject.toml), so a healthy install
always has it; reporting itself stays opt-in via MAGENT_SENTRY_DSN.
When the DSN is unset: zero work, zero imports (the SDK loads only inside
init_sentry, keeping `--help` startup lean). When set: init with
errors-only (no traces), no PII, logging integration at ERROR level
(captures every logger.exception), excepthook, and threading integrations.

If the SDK is somehow missing anyway (a broken/partial install), init
degrades to a rotating-log warning — never a console nag: init runs at CLI
entry for EVERY command, so a stderr line would tax unrelated commands
(`attach`, `status`, …). The actionable repair hint surfaces in
`magent doctor`.

Import this module in-body at CLI entry, after env validation.
"""

from __future__ import annotations

import logging

from magent.log import get_logger

# Repair hint for the should-never-happen state: sentry-sdk ships as a base
# dependency, so its absence means the install itself is damaged.
SENTRY_INSTALL_HINT = "pip install --force-reinstall magent-multi-ai-agents-manager"


def sdk_installed() -> bool:
    """True when the bundled ``sentry-sdk`` package is importable — probed
    via find_spec so the check never actually imports the SDK. Used by
    `magent doctor` to surface a broken install (SDK missing)."""
    from importlib.util import find_spec

    try:
        return find_spec("sentry_sdk") is not None
    except (ImportError, ValueError):
        # ValueError: sys.modules["sentry_sdk"] is None (how tests simulate
        # the package being absent).
        return False


def _release() -> str | None:
    """The installed distribution version, tagged on every event as the Sentry
    release so a report is pinned to a build -- not to whatever git HEAD the
    process happened to run beside. ``None`` (Sentry omits the tag) when the
    package isn't installed as a distribution, e.g. a bare source checkout."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("magent")
    except PackageNotFoundError:
        return None


def init_sentry(dsn: str) -> None:
    """Initialize Sentry SDK with the given DSN. Call once at CLI entry."""
    import atexit

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.threading import ThreadingIntegration
    except ImportError:
        # Should be unreachable: sentry-sdk is a base dependency. Reachable
        # only from a broken/partial install, so degrade quietly by design
        # (never a stderr nag on every command); doctor carries the hint.
        get_logger("sentry").warning(
            "MAGENT_SENTRY_DSN is set but sentry-sdk is missing -- error "
            "reporting is OFF. sentry-sdk ships with magent, so this "
            "install looks broken. Repair: %s",
            SENTRY_INSTALL_HINT,
        )
        return

    sentry_sdk.init(
        dsn=dsn,
        # magent is a single-operator dev tool today (CI never inits Sentry),
        # so the environment is fixed to "dev" -- without minting a new MAGENT_
        # env var, which would break the closed-schema contract for one knob. A
        # future deployment story (prod/staging) would parameterize this.
        environment="dev",
        release=_release(),
        # server_name (hostname) + sys.argv ride along via Sentry's default
        # integrations. Kept on purpose: this is a self-hosted, single-operator
        # setup where "which machine, what command" is exactly the debugging
        # context wanted, and send_default_pii=False is the actual PII gate.
        traces_sample_rate=0,
        send_default_pii=False,
        integrations=[
            LoggingIntegration(level=None, event_level=logging.ERROR),
            ThreadingIntegration(propagate_hub=True),
        ],
    )
    atexit.register(lambda: sentry_sdk.flush(timeout=2))
