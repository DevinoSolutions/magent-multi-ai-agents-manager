"""Sentry error reporting — env-gated, default OFF; SDK bundled.

sentry-sdk is a base dependency (see pyproject.toml), so a healthy install
always has it; reporting itself stays opt-in via MAGENT_SENTRY_DSN.
When the DSN is unset: zero work, zero imports (the SDK loads only inside
init_sentry, keeping `--help` startup lean). When set: init with
errors-only (no traces), no PII, no frame locals, no breadcrumbs, no Sentry
Logs, logging integration at ERROR level (captures every logger.exception),
excepthook, and threading integrations. What that means on each SDK major --
the options are spelled differently -- lives in `_privacy_options` and
`_logging_integration`, and is pinned against the REAL SDK in
tests/unit/test_sentry_real_sdk.py.

If the SDK is somehow missing anyway (a broken/partial install), init
degrades to a rotating-log warning — never a console nag: init runs at CLI
entry for EVERY command, so a stderr line would tax unrelated commands
(`attach`, `status`, …). The actionable repair hint surfaces in
`magent doctor`. An SDK that rejects an option (a future major renaming one)
degrades the same way: reporting is off, the command still runs.

Every path out of `init_sentry` records an `InitOutcome` (active / SDK
missing / rejected, with the SDK version and the exception TYPE -- never the
DSN or the message), and `magent doctor` reads that record rather than
re-deriving it: one answer to "is reporting actually on?".

Import this module in-body at CLI entry, after env validation.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from sentry_sdk._types import Event, Hint

# Repair hint for the should-never-happen state: sentry-sdk ships as a base
# dependency, so its absence means the install itself is damaged.
SENTRY_INSTALL_HINT = "pip install --force-reinstall magent-multi-ai-agents-manager"


class ReportingState(enum.Enum):
    """What `init_sentry` did with a DSN, as `magent doctor` reports it."""

    ACTIVE = "active"  # the SDK accepted the DSN and the options
    SDK_MISSING = "sdk-missing"  # sentry-sdk would not import: a broken install
    REJECTED = "rejected"  # the SDK refused the DSN or an option: reporting is off


@dataclass(frozen=True)
class InitOutcome:
    """The recorded result of `init_sentry`.

    Deliberately NOT carried: the DSN and the exception's message. An SDK
    exception's text is built from the DSN it was handed (BadDsn echoes the
    project segment, a bad port echoes the port text), so the only thing kept
    about a rejection is the exception's TYPE name -- the same rule as the log
    line. Nothing here can leak what it never held.
    """

    state: ReportingState
    sdk_version: str | None = None
    error_type: str | None = None


# One process, one init (CLI entry), one outcome: written by `init_sentry` on
# every path out of it, read by `magent doctor` -- which runs in that same
# process, after the group callback. A second reader re-deriving this (parsing
# the DSN, re-initialising the SDK) would be a second answer to "is reporting
# on?", free to disagree with the first.
_outcome: InitOutcome | None = None


def _record(outcome: InitOutcome) -> None:
    global _outcome  # noqa: PLW0603  # reason: module-level outcome of the one CLI-entry init, same singleton pattern as env._cached_env
    _outcome = outcome


def init_outcome() -> InitOutcome | None:
    """What `init_sentry` recorded in THIS process; ``None`` when it has not
    run here (no DSN, or no CLI entry -- e.g. a library import)."""
    return _outcome


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


def _sdk_major(sentry_sdk: ModuleType) -> int:
    """The installed SDK's major version; 2 when it cannot be read (every real
    SDK has ``VERSION``, and 2.x is the spelling the floor supports)."""
    major = str(getattr(sentry_sdk, "VERSION", "2")).partition(".")[0]
    return int(major) if major.isdigit() else 2


def _privacy_options(sentry_sdk: ModuleType) -> dict[str, object]:
    """The no-PII, no-frame-locals, no-breadcrumbs init options, spelled for
    the installed SDK.

    sentry-sdk 3.0 replaced ``send_default_pii`` and ``include_local_variables``
    with one structured ``data_collection`` option (the old names now raise
    ``TypeError: Unknown option``), so the spellings are chosen by the SDK's
    major version. Both mean the same thing here, and all of it is a privacy
    guarantee rather than tuning:

    * no PII (``send_default_pii=False`` / ``user_info=False``);
    * never ship frame locals. The SDK's scrubber redacts by key NAME only
      (token/secret/auth...), so a node payload in a local named
      ``stdin``/``input_bytes`` -- a GitHub token, say -- rode along verbatim
      in every RemoteError's event. Errors-only reporting never needed locals;
      the traceback stays. Reproduction:
      test_remote_mux.py::TestASecretNeverReachesSentry;
    * no breadcrumbs, on either major (``max_breadcrumbs=0``). A breadcrumb is
      a trail of what happened BEFORE the error -- INFO log lines naming
      projects, user paths and ssh hosts, outgoing-http URLs with their query
      string -- and ``scrub_event`` cuts link secrets only, not those. The
      error and its traceback are all an errors-only report needs.
    """
    if _sdk_major(sentry_sdk) >= 3:
        # 3.x's `data_collection` has no "everything off" switch: a category
        # not named here keeps its (collecting) default, and a new SDK release
        # can add one. So every category the 3.0 SDK defines is closed
        # explicitly. The one key left alone, `frame_context_lines`, is the
        # source text around a traceback frame -- code, not user data, and sent
        # by 2.x too. tests/unit/test_sentry_real_sdk.py walks the SDK's
        # RESOLVED options and fails if any category is still on, so one added
        # upstream turns that test red until it is closed here.
        off = {"mode": "off"}
        return {
            "max_breadcrumbs": 0,
            "data_collection": {
                "user_info": False,
                "stack_frame_variables": False,
                "http_bodies": [],
                "cookies": off,
                "http_headers": {"request": off},
                "url_query_params": off,
                "graphql": {"document": False, "variables": False},
                "gen_ai": {"inputs": False, "outputs": False},
                "mcp": {"inputs": False, "outputs": False},
                "database_query_data": False,
                "queues": False,
            },
        }
    return {
        "send_default_pii": False,
        "include_local_variables": False,
        "max_breadcrumbs": 0,
    }


def _logging_integration(
    sentry_sdk: ModuleType, integration: Callable[..., object]
) -> object:
    """``LoggingIntegration`` that turns ERROR records into events and does
    nothing else, whichever major is installed.

    2.x: ``level`` is the BREADCRUMB level, so ``level=None`` disables log
    breadcrumbs, and there is no Sentry Logs product. 3.x: ``level`` became
    the Sentry-Logs level and a separate ``breadcrumb_level`` (default INFO)
    took over breadcrumbs, so BOTH are None there -- left alone, every INFO
    line magent logs would ride along on an event. 2.x would raise TypeError
    on that extra keyword, hence the split.
    """
    if _sdk_major(sentry_sdk) >= 3:
        return integration(level=None, event_level=logging.ERROR, breadcrumb_level=None)
    return integration(level=None, event_level=logging.ERROR)


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
        _record(InitOutcome(ReportingState.SDK_MISSING))
        return

    # `Callable[..., None]`: the privacy options are spelled per SDK major (see
    # _privacy_options), so no single static signature of `init` covers both.
    init = cast("Callable[..., None]", sentry_sdk.init)
    try:
        init(
            dsn=dsn,
            # magent is a single-operator dev tool today (CI never inits
            # Sentry), so the environment is fixed to "dev" -- without minting
            # a new MAGENT_ env var, which would break the closed-schema
            # contract for one knob. A future deployment story (prod/staging)
            # would parameterize this.
            environment="dev",
            release=_release(),
            # server_name (hostname) + sys.argv ride along via Sentry's default
            # integrations. Kept on purpose: this is a self-hosted,
            # single-operator setup where "which machine, what command" is
            # exactly the debugging context wanted, and the no-PII setting
            # below is the actual PII gate.
            traces_sample_rate=0,
            # ...and never a link credential (scrub_event above).
            before_send=scrub_event,
            integrations=[
                _logging_integration(sentry_sdk, LoggingIntegration),
                # `propagate_scope` is the name on every supported SDK (2.0
                # renamed it from `propagate_hub`; 3.0 removed the old
                # spelling).
                ThreadingIntegration(propagate_scope=True),
            ],
            **_privacy_options(sentry_sdk),
        )
    except Exception as exc:  # noqa: BLE001  # reason: init runs for every command; any SDK rejection must degrade to a log line, never a crash
        # Init runs at CLI entry for EVERY command, so an SDK that renames or
        # drops an option (3.0 did exactly that) must not take the CLI down
        # with a TypeError: reporting is simply off. Quiet for the same reason
        # as the missing-SDK path -- a stderr line would tax every command --
        # so it goes to the rotating log. That file is what people paste into
        # bug reports, and an SDK exception's text is built from the DSN it
        # was handed (BadDsn echoes the project path segment, a bad port
        # echoes the port text), so only the exception TYPE is logged: never
        # `str(exc)`, and never `exc_info` (a traceback carries the message).
        version = str(getattr(sentry_sdk, "VERSION", "?"))
        get_logger("sentry").warning(
            "sentry-sdk %s rejected magent's init options (%s) -- error "
            "reporting is OFF; check MAGENT_SENTRY_DSN",
            version,
            type(exc).__name__,
        )
        _record(InitOutcome(ReportingState.REJECTED, version, type(exc).__name__))
        return
    atexit.register(lambda: sentry_sdk.flush(timeout=2))
    _record(
        InitOutcome(ReportingState.ACTIVE, str(getattr(sentry_sdk, "VERSION", "?")))
    )
