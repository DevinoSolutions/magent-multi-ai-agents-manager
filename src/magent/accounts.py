"""ccswap read seam: the single owner of every ``ccswap`` subprocess.

The account-routing feature needs two things from the outside world -- what
claude accounts exist and how much of each one's quota is spent -- and one
thing of its own: which account a session was last placed on. Both live here,
behind a shape the planner (``routing.py``) can consume without any of it
running. Same split as ``grid.py`` vs ``tiling.py``: this module owns the I/O
and the vendor, that one owns the math.

Like ``tailnet.py`` (the single owner of every ``tailscale`` probe) and
``psmux.py`` (every psmux subprocess), the point of one owner is that the
hazards are stated once:

- **Every call is a LIST argv, never a shell.** A shell is one more thing
  between magent and the tool, and MSYS rewrites a leading ``/`` on the way
  through (the reason ``fleet.py`` builds slash-commands as argv).
- **Every call is bounded and nothing raises out.** A missing binary, a
  timeout, a non-zero exit, malformed JSON -- each answers
  ``AccountsSnapshot(accounts=(), error=...)``. Routing then degrades to
  "unrouted" and the fleet launches exactly as it does today. This is
  ``psmux.send_keys``' posture: a diagnostic returns a value and logs, it
  never takes its caller down.
- **The output is read once from a single pipe.** On Windows
  ``subprocess.run(capture_output=True, timeout=...)`` is not a real bound --
  on expiry it waits for every pipe write end to close, including ones a
  grandchild still holds (measured at 90s for a 5s timeout in
  ``psmux.probe_control_plane``). stderr is discarded for the same reason.
- **One in-flight ccswap call at a time**, behind a module lock. Not a
  performance question: ``list`` can touch the credential store, so two of
  them racing is a correctness problem, in the same family as
  ``psmux.live_sessions`` being the one liveness enumeration.
- **Every ccswap command magent runs is a READ**, and the list is closed:
  ``list --json --provider claude --profiles``, ``usage refresh``,
  ``config get <key>`` and ``--version``. Never ``switch``, ``auto``, ``map``,
  ``add`` or ``profile hydrate`` -- the last one exists and works, and magent
  still does not call it: hydrating is a mutation of somebody else's store,
  and a bring-up is the worst possible moment to take one. magent reads
  ``eligible`` / ``profileHydrated`` and simply does not route to an account
  that is not ready, reporting the reason ccswap gave rather than re-deriving
  a verdict of its own.
- **No email reaches stdout, a log, or a file magent writes.** ccswap's
  ``label`` IS the account's email address, so it is redacted at PARSE time
  (``redact_email``) and the raw local part is never held in memory for a
  later print site to leak.
- **Nothing in magent ever reads or writes inside the ccswap store.**
  ``ccswap_root()`` exists so ``doctor`` can NAME the path and so a test can
  point it somewhere harmless -- the ``wt_keys.find_settings`` posture, where
  the resolver is a seam precisely so no test ever touches the real thing.

Fields are read additively: an unknown key is ignored and a missing optional
degrades, so ccswap may grow its output freely. One value is never guessed:
a ``utilization`` that is absent or null stays ``None`` and is NEVER read as
0 -- absence is not emptiness, and a planner that confused the two would
route straight at an exhausted account.
"""

from __future__ import annotations

import contextlib
import functools
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from magent.log import get_logger

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

# Console-subsystem children spawned from a windowless process (serve, the
# attention daemon, the hotkey listener) get a brand-new console on Windows --
# i.e. a real, empty, focus-stealing terminal window per call. psmux.py learned
# that the loud way; every spawn here carries the same flag. Read off the
# module rather than hand-defined so this file needs no `sys.platform` branch
# (the attribute only exists on Windows; 0 means "no flags" everywhere else).
_SPAWN_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How long any one ccswap command gets. It is a local CLI reading a local
# store, so this is generous rather than tuned -- the failure it bounds is
# "never answers", not "slow".
PROBE_TIMEOUT_S = 10.0

# How long a killed child gets to be reaped before this gives up on it. Short
# on purpose: the process is already dead, and the only thing that can still
# take time here is a pipe magent has stopped caring about.
_REAP_TIMEOUT_S = 1.0

# Where the computed assignment lives. Deliberately NOT the config file: the
# pin (a project's `account`) is the user's intent and belongs in config, but
# the assignment is machine state derived from live utilization. Writing it
# into the config would make a pin and a guess indistinguishable on disk, and
# would turn a load into a write -- an audited defect this repo does not
# reintroduce. Import-bound, so it is registered in tests/conftest.py's
# _IMPORT_BOUND_PATHS (an env redirect is too late for it).
ACCOUNT_MAP_PATH = Path.home() / ".magent" / "account-map.json"

# The map file's own schema. Bumped when the ENTRY shape changes; a file
# written by a newer magent is left alone rather than half-read.
MAP_SCHEMA = 1

# ccswap's default store. Nothing in magent reads or writes inside it.
_CCSWAP_DIR_NAME = ".claude-swap-backup"

# The account kind magent routes to. An `api-key` slot bills the API instead of
# a subscription, which is the opposite of this feature's whole purpose.
SUBSCRIPTION_KIND = "subscription"

# The JSON contract magent reads. ccswap promises fields are ADDITIVE within a
# version, so an unknown key is ignored -- but a HIGHER version is a shape this
# magent has not been taught, and guessing at it is how a reader invents data.
# An absent version is an older ccswap and is read as 1.
CCSWAP_SCHEMA_VERSION = 1

# The ccswap build this contract was written against (`ccswap --version` prints
# "ccswap <version>"). Older builds predate the read-only `--profiles` mode and
# the per-profile health fields.
MIN_CCSWAP_VERSION = "0.31.0+pr308.2"

# ccswap's closed vocabulary for WHY an account cannot be used, mapped to the
# wording magent shows. The code itself is authoritative and is carried
# verbatim; this only translates. An unknown code is shown as ccswap sent it,
# because a reason magent cannot translate is still a reason the user needs.
INELIGIBLE_REASONS: dict[str, str] = {
    "disabled": "disabled in ccswap",
    "api_key": "an API-key slot, not a subscription",
    "relogin_required": "needs a fresh login",
    "no_credentials": "has no stored credentials",
    "foreign_credential": "holds another account's credential",
    "keychain_unavailable": "its keychain could not be read",
}

# The ccswap settings magent requires and NEVER changes, as
# `(setting, wanted, why, the command that sets it)`. Each is read with
# `ccswap config get <key>`, which answers a strict boolean. Both defaults are
# the value magent cannot work with -- persistent profiles default OFF and
# autoswitch defaults ON -- so a fresh ccswap needs both flipped, by the user.
# A refusal that does not say how to fix itself is a dead end; magent warns and
# skips, and the user's own configuration always wins (the `wt-keys` posture).
REQUIRED_SETTINGS: tuple[tuple[str, bool, str, str], ...] = (
    (
        "profiles.persistent",
        True,
        "profiles must stay launchable between runs",
        "ccswap config set profiles.persistent true",
    ),
    (
        "autoswitch.enabled",
        False,
        "a global switch fighting magent's placement is silent and fleet-wide",
        "ccswap config set autoswitch.enabled false",
    ),
)

# Serializes every ccswap invocation this process makes. See the module note.
_CCSWAP_LOCK = threading.Lock()


def _log() -> logging.Logger:
    # The launch log: everything this module answers is consumed by a bring-up
    # or by a command explaining one, and a sixth log name would split that
    # story across two files.
    return get_logger("launch")


def ccswap_root() -> Path:
    """ccswap's store directory, ``~/.claude-swap-backup``.

    A function and not a constant on purpose: an import-bound ``Path.home()``
    is computed once, before any environment redirect can reach it -- the
    defect class tests/conftest.py's tripwire exists to catch, and this path
    is one the tripwire specifically watches. Nothing in magent reads or
    writes inside it; it exists so a diagnostic can name it.
    """
    return Path.home() / _CCSWAP_DIR_NAME


@functools.lru_cache(maxsize=1)
def find_ccswap() -> str | None:
    """Locate the ccswap binary, or None. LRU-cached for the process lifetime.

    Mirrors ``psmux.find_psmux``, including its caveat: a test that changes
    PATH must call ``find_ccswap.cache_clear()`` on the way IN and OUT.
    """
    return shutil.which("ccswap")


@dataclass(frozen=True)
class Window:
    """One quota window's reading.

    ``utilization`` is a 0-1 fraction or None, and None means UNKNOWN -- never
    "empty". ``resets_at`` is epoch seconds or None. ``status`` carries
    ccswap's own word for the window when it has one, verbatim.
    """

    utilization: float | None = None
    resets_at: float | None = None
    status: str = ""


EMPTY_WINDOW = Window()


@dataclass(frozen=True)
class Account:
    """One claude account as ccswap reports it.

    ``eligible`` / ``ineligible_reason`` are ccswap's verdict and magent
    honours them rather than re-deriving one: the code is carried verbatim
    (``INELIGIBLE_REASONS`` translates it for display). ``scoped`` holds
    per-model-class caps keyed by a lower-cased scope name (``"fable"`` today).

    ``label`` is REDACTED at parse time -- ccswap's label is the account's
    email address, and no email may reach stdout, a log or a file magent
    writes. The raw address is never held in memory here at all, which is the
    only version of that rule that cannot be forgotten at a print site.

    The four ``profile_*`` health fields are the early-warning surface for the
    hazard this feature has to live with: ccswap's store-to-profile write-back
    is skipped while a live session pid exists, and a resident magent fleet IS
    a permanently live pid on every routed account. A profile whose identity
    drifted, whose records will not parse, or whose token is about to expire is
    one on its way to a dead slot, and `doctor` should say so before it is.
    """

    id: str
    label: str = ""
    kind: str = ""
    active: bool = False
    profile_dir: str = ""
    hydrated: bool = False
    eligible: bool = False
    ineligible_reason: str | None = None
    five_hour: Window = EMPTY_WINDOW
    seven_day: Window = EMPTY_WINDOW
    scoped: Mapping[str, Window] = field(default_factory=dict)
    overage_status: str = ""
    live_sessions: int = 0
    unreadable_records: int = 0
    identity_drifted: bool = False
    token_expires_at: float | None = None

    @property
    def ineligible_text(self) -> str:
        """``ineligible_reason`` in magent's words, or ccswap's own if it is a
        code this magent has not been taught."""
        code = self.ineligible_reason or ""
        return INELIGIBLE_REASONS.get(code, code)


@dataclass(frozen=True)
class AccountsSnapshot:
    """Everything one ccswap read produced -- including the reasons it produced
    nothing. ``error`` is set and ``accounts`` empty for every failure mode, so
    a caller has exactly one thing to check.

    ``usage_age_s`` is how old ccswap's usage numbers are; ``stale_ok_s`` is
    how old ccswap itself considers acceptable, so a caller can prefer the
    vendor's own budget over a magent-side guess. ``duplicate_warnings``
    non-empty means two slots claim the same login, which is a refuse-to-route
    condition for the caller -- placement assumes accounts are distinct.
    """

    accounts: tuple[Account, ...] = ()
    usage_age_s: float | None = None
    serve_ttl_s: float | None = None
    stale_ok_s: float | None = None
    duplicate_warnings: tuple[str, ...] = ()
    schema_version: int = CCSWAP_SCHEMA_VERSION
    error: str | None = None

    def by_id(self) -> dict[str, Account]:
        return {a.id: a for a in self.accounts}


@dataclass(frozen=True)
class SettingsReport:
    """The C-2 check: are ccswap's settings ones magent can work under.

    ``problems`` is empty when every required setting reads the value magent
    needs. ``error`` means the question could not be ASKED -- which is not the
    same as an answer, and a caller must not treat it as permission.
    """

    values: Mapping[str, bool | None] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.error is None


@dataclass(frozen=True)
class MapEntry:
    """One session's recorded placement, as stored in ``account-map.json``.

    ``class_source`` is ``"config"`` | ``"observed"`` | ``"default"`` -- how
    the model class was arrived at, which is what lets the next plan trust an
    observation and distrust a guess.
    """

    account: str
    klass: str = "standard"
    class_source: str = "default"
    assigned_at: str = ""
    reason: str = ""
    observed_model: str = ""


# --- narrowing helpers --------------------------------------------------------
# json.loads answers with whatever the file said, so every read below narrows
# explicitly. `typing.Any` is banned repo-wide; these are the isinstance walls.


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def redact_email(text: str) -> str:
    """``"amin.dhouib@outlook.com"`` -> ``"<user>@outlook.com"``.

    The domain is kept because it is what distinguishes one slot from another
    to a human reading a table; the local part is the identity and never
    leaves this function. Applied at PARSE time, so the raw address is not
    held in memory for a later print site to leak -- the only form of "no
    email reaches stdout or a log" that cannot be forgotten.
    """
    local, at, domain = text.partition("@")
    if not at or not local:
        return text
    return f"<user>@{domain}" if domain else "<user>"


def _as_fraction(value: object) -> float | None:
    """A 0-1 utilization reading, or None for anything that is not a number.

    ``bool`` is excluded deliberately (it is an ``int`` subclass, and
    ``True`` is not 100% of anything). Out-of-range values are clamped rather
    than dropped: a reading past 1.0 means "over the cap", which is a fact
    worth keeping, not a parse error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, float(value))


def _as_epoch(value: object) -> float | None:
    """An ISO-8601 timestamp as epoch seconds, or None.

    ``datetime.fromisoformat`` only learned to read a trailing ``Z`` in 3.11
    and this package supports 3.10, so the offset is normalised by hand. A
    timestamp with no offset at all is read as UTC, which is what ccswap
    emits; a naive local reading would be wrong by the machine's offset.
    """
    text = _as_str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _window(value: object) -> Window:
    raw = _as_dict(value)
    if not raw:
        return EMPTY_WINDOW
    return Window(
        utilization=_as_fraction(raw.get("utilization")),
        resets_at=_as_epoch(raw.get("resetsAt")),
        status=_as_str(raw.get("status")),
    )


def _scoped_windows(value: object) -> dict[str, Window]:
    """``usage.scoped[]`` keyed by its lower-cased scope name.

    A later entry for the same scope wins, and an entry with no scope name is
    dropped -- an unnamed cap cannot be matched to a model class, and guessing
    which one it belongs to is exactly the kind of invention this module does
    not do.
    """
    out: dict[str, Window] = {}
    for item in _as_list(value):
        raw = _as_dict(item)
        scope = _as_str(raw.get("scope")).strip().lower()
        if scope:
            out[scope] = _window(raw)
    return out


def _account(value: object) -> Account | None:
    raw = _as_dict(value)
    acct_id = _as_str(raw.get("id")).strip()
    if not acct_id:
        return None  # an account magent cannot name is an account it cannot use
    usage = _as_dict(raw.get("usage"))
    reason = raw.get("ineligibleReason")
    return Account(
        id=acct_id,
        label=redact_email(_as_str(raw.get("label"))),
        kind=_as_str(raw.get("kind")),
        active=_as_bool(raw.get("active")),
        profile_dir=_as_str(raw.get("profilePath")),
        hydrated=_as_bool(raw.get("profileHydrated")),
        eligible=_as_bool(raw.get("eligible")),
        ineligible_reason=_as_str(reason) or None,
        five_hour=_window(usage.get("fiveHour")),
        seven_day=_window(usage.get("sevenDay")),
        scoped=_scoped_windows(usage.get("scoped")),
        overage_status=_as_str(raw.get("overageStatus")),
        live_sessions=_as_int(raw.get("profileLiveSessions")),
        unreadable_records=_as_int(raw.get("profileUnreadableRecords")),
        identity_drifted=_as_bool(raw.get("profileIdentityDrifted")),
        token_expires_at=_as_epoch(raw.get("profileTokenExpiresAt")),
    )


def _as_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _run(args: list[str], timeout: float) -> tuple[int, str] | None:
    """One bounded ccswap invocation. ``(returncode, stdout)``, or None when it
    could not be spawned or outran the clock. Never raises."""
    with _CCSWAP_LOCK:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_SPAWN_FLAGS,
            )
        except OSError:
            _log().warning("ccswap: could not run %r", args[:2], exc_info=True)
            return None
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Reap the direct child ONLY, and bounded. The obvious
            # `communicate()` here is the trap: it waits for every pipe WRITE
            # end to close, and the interpreter behind a `.cmd`/shell shim is a
            # GRANDCHILD that still holds one -- measured at the stalled fake's
            # full 120s lifetime against a 2s timeout, which is the same defect
            # psmux.probe_control_plane found at 90s-for-5s. `wait` waits on the
            # process handle instead, which the kill has already settled.
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=_REAP_TIMEOUT_S)
            _log().warning(
                "ccswap: %r did not answer within %.1fs (waited %.1fs)",
                args[1:3],
                timeout,
                time.monotonic() - started,
            )
            return None
        return proc.returncode, out.decode("utf-8", "replace")


def read_accounts(
    *, ccswap: str | None = None, timeout: float = PROBE_TIMEOUT_S
) -> AccountsSnapshot:
    """The one read: every claude account ccswap knows, with its usage.

    ``ccswap list --json --provider claude --profiles`` -- read-only by
    contract, which is why ``--profiles`` exists as its own mode (plain `list`
    is NOT side-effect-free: it rewrites the active account's live credential
    and can POST the store's refresh grant, which is how a slot dies). Never
    raises: every failure comes back as a snapshot carrying ``error``, because
    a bring-up that cannot reach ccswap must still bring the fleet up.
    """
    binary = ccswap or find_ccswap()
    if not binary:
        return AccountsSnapshot(error="ccswap is not installed (not on PATH)")
    result = _run(
        [binary, "list", "--json", "--provider", "claude", "--profiles"], timeout
    )
    if result is None:
        return AccountsSnapshot(
            error=f"ccswap did not answer within {timeout:.0f}s (or could not run)"
        )
    rc, out = result
    if rc != 0:
        return AccountsSnapshot(error=f"ccswap list exited {rc}")
    try:
        payload = json.loads(out)
    except ValueError:
        return AccountsSnapshot(error="ccswap list did not return JSON")
    body = _as_dict(payload)
    if not body:
        return AccountsSnapshot(error="ccswap list returned no object")
    schema = _as_int(body.get("schemaVersion"), CCSWAP_SCHEMA_VERSION)
    if schema > CCSWAP_SCHEMA_VERSION:
        # Fields are additive WITHIN a version; a higher one is a shape this
        # magent was not taught, and reading it anyway is how a parser invents
        # data. Refusing degrades to "unrouted", which is always safe.
        return AccountsSnapshot(
            schema_version=schema,
            error=(
                f"ccswap speaks list schema {schema}; this magent knows "
                f"{CCSWAP_SCHEMA_VERSION} — upgrade magent"
            ),
        )
    accounts = tuple(
        acct
        for acct in (_account(item) for item in _as_list(body.get("accounts")))
        if acct is not None
    )
    cache = _as_dict(body.get("usageCache"))
    return AccountsSnapshot(
        accounts=accounts,
        usage_age_s=_usage_age(cache),
        serve_ttl_s=_as_seconds(cache.get("serveTtlSeconds")),
        stale_ok_s=_as_seconds(cache.get("staleOkSeconds")),
        duplicate_warnings=tuple(
            _as_str(w)
            for w in _as_list(body.get("duplicateAccountWarnings"))
            if _as_str(w)
        ),
        schema_version=schema,
    )


def _usage_age(cache: Mapping[str, object]) -> float | None:
    """How old ccswap's usage numbers are, in seconds.

    ``ageSeconds`` when ccswap computes it, else derived from ``fetchedAt``
    against the wall clock -- this is the I/O layer, so reading a clock here is
    fine; the PLANNER is the thing that must stay free of one. A fetch stamped
    in the future reads as 0 rather than negative.
    """
    for key in ("ageSeconds", "ageS"):
        age = _as_seconds(cache.get(key))
        if age is not None:
            return max(0.0, age)
    fetched = _as_epoch(cache.get("fetchedAt"))
    if fetched is None:
        return None
    return max(0.0, time.time() - fetched)


# ccswap's own floor for `usage refresh --max-age`; asking for fresher than
# this is asking it to hammer the API, and it clamps anyway.
REFRESH_MIN_MAX_AGE_S = 30


def refresh_usage(
    max_age_s: float, *, ccswap: str | None = None, timeout: float = PROBE_TIMEOUT_S
) -> bool:
    """Ask ccswap to re-read usage older than ``max_age_s``. True on success.

    NEVER on the launch path. A bring-up must not block on somebody else's
    network read, and launch-time assignment is exactly the use ccswap's own
    cache is adequate for -- this is for an interactive ``magent account``,
    at most once per invocation.

    ``--max-age`` is clamped to ccswap's 30s floor here as well as there, so
    the argv magent builds is one ccswap would not have to correct.
    """
    binary = ccswap or find_ccswap()
    if not binary:
        return False
    max_age = max(REFRESH_MIN_MAX_AGE_S, int(max_age_s))
    result = _run(
        [binary, "usage", "refresh", "--max-age", str(max_age), "--json"], timeout
    )
    return result is not None and result[0] == 0


def read_settings(
    *, ccswap: str | None = None, timeout: float = PROBE_TIMEOUT_S
) -> SettingsReport:
    """Are ccswap's settings ones magent can route under? One reader, one
    answer -- `magent account` and `doctor` must not each derive their own.

    ``ccswap config get <key>`` per REQUIRED_SETTINGS, each a read and each
    bounded. A key that cannot be read at all is an ERROR, not a pass: magent
    must not treat "could not ask" as "the answer was yes". A key that reads
    the wrong value is a problem line naming the setting, why magent needs it,
    and the exact command that sets it -- magent never flips one itself.
    """
    binary = ccswap or find_ccswap()
    if not binary:
        return SettingsReport(error="ccswap is not installed (not on PATH)")
    values: dict[str, bool | None] = {}
    problems: list[str] = []
    unreadable: list[str] = []
    for key, wanted, why, fix in REQUIRED_SETTINGS:
        result = _run([binary, "config", "get", key], timeout)
        current = _as_strict_bool(result[1]) if result and result[0] == 0 else None
        values[key] = current
        if current is None:
            unreadable.append(key)
            continue
        if current is not wanted:
            problems.append(
                f"ccswap {key} is {str(current).lower()}, magent needs "
                f"{str(wanted).lower()} ({why}); run: {fix}"
            )
    return SettingsReport(
        values=values,
        problems=tuple(problems),
        error=(
            f"could not read ccswap {', '.join(unreadable)}" if unreadable else None
        ),
    )


def _as_strict_bool(text: str) -> bool | None:
    """``"true"``/``"false"`` (as `config get` prints them) and nothing else.

    Anything else is UNKNOWN rather than False: a help banner, an error line or
    an empty answer must never read as "autoswitch is off".
    """
    stripped = text.strip().lower()
    if stripped in ("true", "false"):
        return stripped == "true"
    return None


def read_version(
    *, ccswap: str | None = None, timeout: float = PROBE_TIMEOUT_S
) -> str | None:
    """The version ``ccswap --version`` reports (e.g. ``"0.31.0+pr308.2"``), or
    None when it cannot be asked. The leading program name is stripped."""
    binary = ccswap or find_ccswap()
    if not binary:
        return None
    result = _run([binary, "--version"], timeout)
    if result is None or result[0] != 0:
        return None
    text = result[1].strip().splitlines()[0] if result[1].strip() else ""
    parts = text.split()
    return parts[-1] if parts else None


def _release_tuple(version: str) -> tuple[int, ...]:
    """The numeric release part of a version, as a tuple. Build/pre-release
    metadata (``+pr308.2``, ``-rc1``) is dropped -- it orders nothing that
    magent can reason about."""
    release = version.strip().lstrip("vV").split("+")[0].split("-")[0]
    out: list[int] = []
    for piece in release.split("."):
        if not piece.isdigit():
            break
        out.append(int(piece))
    return tuple(out)


def version_at_least(version: str | None, minimum: str = MIN_CCSWAP_VERSION) -> bool:
    """Is ``version`` new enough to speak this contract?

    Compares the numeric release only. An unreadable version answers False --
    the caller's job is then to WARN, never to refuse: this is a diagnostic,
    and a magent that refused to route because it could not parse a version
    string would be worse than one that tried and reported honestly.
    """
    if not version:
        return False
    theirs, floor = _release_tuple(version), _release_tuple(minimum)
    if not theirs or not floor:
        return False
    return theirs >= floor


def config_dir(acct: Account) -> str:
    """The account's profile directory -- its ``CLAUDE_CONFIG_DIR``.

    One value, two names: ccswap calls it the profile dir, claude calls it the
    config dir, and they are the same directory. Empty when ccswap reported
    none, which callers must read as "this account cannot be routed to".
    """
    return acct.profile_dir


def profile_env(acct: Account) -> dict[str, str]:
    """The environment overlay that puts a pane on this account.

    The account is ENVIRONMENT, not a command line: an agent command is typed
    into a pane by send-keys and magent never sees its exit code, so prefixing
    the command could never be verified. Empty when the account has no profile
    dir -- an overlay that names nothing must not be attached.
    """
    target = config_dir(acct)
    return {"CLAUDE_CONFIG_DIR": target} if target else {}


# --- the assignment map -------------------------------------------------------


def _entry(value: object) -> MapEntry | None:
    raw = _as_dict(value)
    account = _as_str(raw.get("account")).strip()
    if not account:
        return None
    return MapEntry(
        account=account,
        klass=_as_str(raw.get("class"), "standard"),
        class_source=_as_str(raw.get("classSource"), "default"),
        assigned_at=_as_str(raw.get("assignedAt")),
        reason=_as_str(raw.get("reason")),
        observed_model=_as_str(raw.get("observedModel")),
    )


def now_stamp() -> str:
    """The timestamp a ``MapEntry`` records, in the map file's own format.

    One spelling, here, so a record written by the launch path and the
    ``updatedAt`` ``write_map`` stamps cannot disagree about what a time is.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_map(path: Path | None = None) -> dict[str, MapEntry]:
    """The recorded placement per psmux session id, or ``{}``.

    Keyed by session id -- the key ``sessions --json``, ``status`` and the
    fleet commands already use -- so nothing has to translate. Absent,
    unreadable, malformed, or written to a schema this magent does not know:
    all answer ``{}``. A prior map is an optimisation (stickiness), never a
    dependency, so losing it costs a re-assignment and nothing else.
    """
    target = path or ACCOUNT_MAP_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    body = _as_dict(payload)
    schema = body.get("schema")
    if schema != MAP_SCHEMA:
        _log().warning(
            "account map %s is schema %r, not %d; ignoring it",
            target,
            schema,
            MAP_SCHEMA,
        )
        return {}
    out: dict[str, MapEntry] = {}
    for session, value in _as_dict(body.get("entries")).items():
        entry = _entry(value)
        if entry is not None:
            out[session] = entry
    return out


def write_map(entries: Mapping[str, MapEntry], path: Path | None = None) -> bool:
    """Persist the assignment map atomically. False (and a log line) on any
    failure -- this is a cache of a decision, never the decision itself, so a
    disk that will not take it must not fail a bring-up."""
    target = path or ACCOUNT_MAP_PATH
    body = {
        "schema": MAP_SCHEMA,
        "updatedAt": now_stamp(),
        "entries": {
            session: {
                "account": e.account,
                "class": e.klass,
                "classSource": e.class_source,
                "assignedAt": e.assigned_at,
                "reason": e.reason,
                "observedModel": e.observed_model,
            }
            for session, e in entries.items()
        },
    }
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        _log().warning("could not write the account map %s", target, exc_info=True)
        return False
    return True
