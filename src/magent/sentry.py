"""Sentry error reporting — env-gated, default OFF; SDK bundled.

sentry-sdk is a base dependency (see pyproject.toml), so a healthy install
always has it; reporting itself stays opt-in via MAGENT_SENTRY_DSN.
When the DSN is unset: zero work, zero imports (the SDK loads only inside
init_sentry, keeping `--help` startup lean). When set: init with
errors-only (no traces), no PII, no frame locals, logging integration at ERROR level
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
import re
from typing import TYPE_CHECKING, cast

from magent.log import get_logger

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

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


# Never shipped to Sentry: the link's credentials (`magent link`). Keys are
# matched case-insensitively, camelCase spellings included; in any string, a
# Bearer value, a host or poll token (``mlh_``/``mlp_`` + base64url) and the
# pairing QR link's secret (``s=`` in its fragment) are cut out wherever they
# sit.
_SECRET_KEYS = frozenset(
    {"authorization", "host_token", "poll_token", "hosttoken", "polltoken"}
)
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s\"',;)]+")
_TOKEN_RE = re.compile(r"ml[hp]_[A-Za-z0-9_-]+")
_QR_SECRET_RE = re.compile(r"(?<=[#&]s=)[A-Za-z0-9_-]+")
REDACTED = "[redacted]"


def _scrub_text(text: str) -> str:
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _TOKEN_RE.sub(REDACTED, text)
    return _QR_SECRET_RE.sub(REDACTED, text)


def _scrub(value: object, key: str = "") -> object:
    lowered = key.lower()
    if lowered in _SECRET_KEYS or lowered.endswith("priv"):
        return REDACTED
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    return value


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """Sentry ``before_send``: the event with every link secret redacted --
    Authorization headers and Bearer values, host and poll tokens, private
    keys, the QR link's secret -- in messages, breadcrumbs, exception
    values, request data and extras alike."""
    del hint  # the SDK's signature; nothing in it is sent
    scrubbed = _scrub(event)
    return cast("Event", scrubbed) if isinstance(scrubbed, dict) else event


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
        # Never ship frame locals. sentry-sdk 2.x defaults this to True and
        # its scrubber redacts by key NAME only (token/secret/auth...), so a
        # node payload in a local named `stdin`/`input_bytes` -- a GitHub
        # token, say -- rode along verbatim in every RemoteError's event.
        # Errors-only reporting never needed locals; the traceback stays.
        include_local_variables=False,
        # ...and never a link credential (scrub_event above).
        before_send=scrub_event,
        integrations=[
            LoggingIntegration(level=None, event_level=logging.ERROR),
            ThreadingIntegration(propagate_hub=True),
        ],
    )
    atexit.register(lambda: sentry_sdk.flush(timeout=2))
