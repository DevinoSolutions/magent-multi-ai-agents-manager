"""The account-assignment planner: which account each project should run on.

PURE. No subprocess, no filesystem, no clock beyond the ``now`` it is handed.
The same ``(projects, snapshot, policy, prior_map, now)`` always yields a
byte-identical ``Plan`` -- no hashing, no randomness, no set iteration order --
which is what lets ``magent account plan`` be a truthful dry run of what a
launch will do. A planner whose preview can disagree with the real thing is
worse than no preview at all.

This is ``grid.py`` to ``accounts.py``'s ``tiling.py``: the math lives here so
the whole algorithm is unit-testable with no ccswap installed, and so the data
SOURCE can be replaced (live rate-limit headers instead of ccswap's ~10-minute
cache) without touching a line of policy.

Inputs are plain values -- ``Project`` and ``Policy`` below -- not config
objects. The planner must not depend on a config schema, both because it
predates one and because "what the user typed" and "what the planner needs"
are different questions; the mapping between them belongs to whoever reads
the config.

The two loud rules, each learned from a measurement:

- **Absence is not zero.** A window with no reading is UNKNOWN. An account
  whose binding window is unknown never receives NEW work (a non-200 carries
  no rate-limit headers at all, so a tracker reading absence as 0% routes
  straight at an exhausted account) -- but it never EVICTS a project already
  on it either, because unknown is not evidence of exhaustion.
- **Stickiness beats optimality.** Moving a project costs a measured ~10.5k
  extra cache-creation tokens. Chasing a few points of headroom loses more
  than it wins, so a placement is kept until the account it names becomes
  genuinely unusable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from magent.accounts import EMPTY_WINDOW, SUBSCRIPTION_KIND

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from magent.accounts import Account, AccountsSnapshot, MapEntry, Window

# --- the model classes --------------------------------------------------------
# Two, deliberately. `fable` names the work that consumes a model-scoped weekly
# cap of its own; `standard` is everything else. The asymmetry is the whole
# point of the split: a standard project placed on a Fable-exhausted account
# merely drains 7-day headroom that was otherwise wasted, which is the goal --
# a fable project placed there is simply blocked.
CLASS_FABLE = "fable"
CLASS_STANDARD = "standard"
CLASSES = (CLASS_FABLE, CLASS_STANDARD)

# Where a class came from. `observed` is a class read off a live pane, and it
# is the only one a later plan is allowed to trust over its own default.
SOURCE_CONFIG = "config"
SOURCE_OBSERVED = "observed"
SOURCE_DEFAULT = "default"

# `standard` is the safe default for exactly the asymmetry above: guessing
# standard costs headroom, guessing fable costs a blocked agent.
DEFAULT_CLASS = CLASS_STANDARD

# What one more session is assumed to add to an account's binding window while
# a single plan is being computed. An honest heuristic and NOT a tunable: it
# exists so a pass that places eight projects spreads them instead of stacking
# all eight on whichever account happened to read lowest. The tie-breakers
# below do the rest of the work.
NOTIONAL_SESSION_LOAD = 0.02

# The closed vocabulary. Every member carries a human string, and the closure
# is what makes the plan table explainable -- the same posture as
# `altv.ALTV_OUTCOMES`. A reason with no entry here is a bug, pinned by test.
REASONS: dict[str, str] = {
    "pinned": "pinned to this account in the config",
    "pinned-over-limit": "pinned to this account, which is over the hard limit",
    "kept": "already on this account, and it is still usable",
    "moved-soft": "its account can no longer take it (not a usage limit)",
    "moved-hard": "its account is at or over the hard limit",
    "assigned": "newly placed on the account with the most headroom",
    "unrouted-no-eligible-account": "no account can take it right now",
    "unrouted-no-data": "ccswap reported nothing usable",
    "unrouted-disabled": "account routing is off",
}

# `settings.accounts.onLimit`, parsed. A closed vocabulary with one parameter.
ON_LIMIT_WAIT = "wait"
ON_LIMIT_MOVE = "move"
ON_LIMIT_MOVE_IF_RESET = "move-if-reset"
DEFAULT_ON_LIMIT = "move-if-reset>2h"

_MOVE_IF_RESET_RE = re.compile(r"^move-if-reset>(\d+(?:\.\d+)?)h$", re.IGNORECASE)


def parse_on_limit(value: str | None) -> tuple[str, float | None]:
    """``"move-if-reset>2h"`` -> ``("move-if-reset", 2.0)``.

    ``"wait"`` and ``"move"`` carry no hours. Anything unrecognised degrades to
    ``("wait", None)`` -- the DO-NOTHING answer, because a misspelled policy
    must not be read as permission to move a live session. Never raises; the
    caller owns the warning.
    """
    text = (value or "").strip().lower()
    if text == ON_LIMIT_MOVE:
        return ON_LIMIT_MOVE, None
    match = _MOVE_IF_RESET_RE.match(text)
    if match:
        return ON_LIMIT_MOVE_IF_RESET, float(match.group(1))
    return ON_LIMIT_WAIT, None


@dataclass(frozen=True)
class AccountPolicy:
    """Per-account overrides. ``klass`` pins an account to ONE model class --
    it then takes only that class's work -- and ``exclude`` takes it out of
    routing entirely without removing it from ccswap."""

    exclude: bool = False
    klass: str | None = None
    on_limit: str = ""


@dataclass(frozen=True)
class Policy:
    """The knobs, in the units the config spells them.

    Thresholds are PERCENT (0-100), matching ``softThreshold`` /
    ``hardThreshold``; a ``Window.utilization`` is a 0-1 fraction, and the one
    conversion happens here rather than at every comparison site.
    """

    enabled: bool = False
    soft_threshold: float = 85.0
    hard_threshold: float = 95.0
    on_limit: str = DEFAULT_ON_LIMIT
    stale_after_s: float = 900.0
    per_account: Mapping[str, AccountPolicy] = field(default_factory=dict)

    def account_policy(self, acct_id: str) -> AccountPolicy:
        return self.per_account.get(acct_id) or AccountPolicy()


@dataclass(frozen=True)
class Project:
    """What the planner needs to know about one project.

    ``account`` is the PIN -- user intent, typed into the config -- and is the
    only account value that is ever an input. What a previous pass decided
    arrives through ``prior_map`` instead, so a pin and a guess can never be
    confused for one another.
    """

    session: str
    name: str = ""
    account: str | None = None
    model_class: str | None = None


@dataclass(frozen=True)
class Row:
    """One project's verdict, with everything needed to explain it."""

    session: str
    project: str
    account: str | None
    klass: str
    class_source: str
    reason: str
    utilization: float | None = None
    resets_at: float | None = None
    warning: str | None = None


@dataclass(frozen=True)
class Plan:
    rows: tuple[Row, ...] = ()
    stale: bool = False
    usage_age_s: float | None = None
    error: str | None = None

    def account_of(self, session: str) -> str | None:
        for row in self.rows:
            if row.session == session:
                return row.account
        return None


def binding_window(acct: Account, klass: str) -> Window:
    """The window that actually constrains ``klass`` on this account.

    ``fable`` is capped by its own model-scoped window as well as the account's
    5-hour and 7-day ones; ``standard`` is NOT -- excluding the fable cap from
    the standard calculation is exactly what lets a Fable-100% account keep
    hosting Opus/Sonnet work, which is the headroom this whole feature exists
    to recover. The binding window is the one with the highest reading; a
    window with no reading is skipped, never counted as 0.
    """
    candidates = [acct.five_hour, acct.seven_day]
    if klass == CLASS_FABLE:
        candidates.append(acct.scoped.get(CLASS_FABLE, EMPTY_WINDOW))
    known = [w for w in candidates if w.utilization is not None]
    if not known:
        return EMPTY_WINDOW
    return max(known, key=lambda w: w.utilization or 0.0)


def _percent(window: Window) -> float | None:
    return None if window.utilization is None else window.utilization * 100.0


def _until(resets_at: float | None, now: float) -> str:
    """ "in 2h 13m" for a reset in the future, "" when unknown or past."""
    if resets_at is None:
        return ""
    remaining = int(resets_at - now)
    if remaining <= 0:
        return ""
    hours, minutes = divmod(remaining // 60, 60)
    return f"in {hours}h {minutes:02d}m" if hours else f"in {minutes}m"


# Why an account cannot take a project. "hard-limit" is the only one that means
# "usage"; every other value is a state of the account itself, which is the
# distinction between a `moved-hard` row and a `moved-soft` one.
_BLOCK_HARD = "hard-limit"


def _blocker(acct: Account, policy: Policy, klass: str, *, placing: bool) -> str | None:
    """Why ``acct`` cannot host a ``klass`` project, or None.

    ``placing`` separates the two questions this module keeps apart. Placing
    NEW work needs a known reading (unknown is not headroom). KEEPING work that
    is already there does not: an account whose reading went unknown has not
    been shown to be exhausted, and evicting on an absent number would move a
    live session for no evidence at all.
    """
    if acct.kind and acct.kind != SUBSCRIPTION_KIND:
        return f"not a subscription account ({acct.kind})"
    if not acct.eligible:
        # ccswap's verdict, in its own words -- magent does not re-derive one.
        return acct.ineligible_text or "ccswap reports it as ineligible"
    if not acct.hydrated:
        return "its profile holds no usable login"
    per = policy.account_policy(acct.id)
    if per.exclude:
        return "excluded in settings.accounts.perAccount"
    if per.klass and per.klass != klass:
        return f"reserved for {per.klass} work"
    percent = _percent(binding_window(acct, klass))
    if percent is None:
        return "no usage reading" if placing else None
    if percent >= policy.hard_threshold:
        return _BLOCK_HARD
    if placing and percent >= policy.soft_threshold:
        # Soft means "stop placing NEW work here", never "evict". A project
        # already on this account keeps it (see `placing=False` above).
        return f"at {percent:.0f}% of its binding window"
    return None


def _classify(project: Project, prior: MapEntry | None) -> tuple[str, str]:
    """``(class, source)``. Config wins; then an OBSERVED class from a previous
    run; then the safe default. A class the map merely defaulted to is not
    evidence, so it never outranks today's default."""
    configured = (project.model_class or "").strip().lower()
    if configured in CLASSES:
        return configured, SOURCE_CONFIG
    if prior is not None and prior.class_source == SOURCE_OBSERVED:
        observed = (prior.klass or "").strip().lower()
        if observed in CLASSES:
            return observed, SOURCE_OBSERVED
    return DEFAULT_CLASS, SOURCE_DEFAULT


def _unrouted_rows(
    projects: Sequence[Project], reason: str, prior_map: Mapping[str, MapEntry]
) -> tuple[Row, ...]:
    """One row per project, routed nowhere. The class is still reported: the
    table says what magent WOULD have done, which is the difference between
    "off" and "broken"."""
    rows: list[Row] = []
    for project in projects:
        klass, source = _classify(project, prior_map.get(project.session))
        rows.append(
            Row(
                session=project.session,
                project=project.name or project.session,
                account=None,
                klass=klass,
                class_source=source,
                reason=reason,
            )
        )
    return tuple(rows)


def _row_for(
    project: Project,
    acct: Account,
    klass: str,
    source: str,
    reason: str,
    warning: str | None,
) -> Row:
    window = binding_window(acct, klass)
    return Row(
        session=project.session,
        project=project.name or project.session,
        account=acct.id,
        klass=klass,
        class_source=source,
        reason=reason,
        utilization=window.utilization,
        resets_at=window.resets_at,
        warning=warning,
    )


def _best_account(
    accounts: Sequence[Account], policy: Policy, klass: str, placed: Mapping[str, int]
) -> Account | None:
    """The eligible account with the most projected headroom for ``klass``.

    Ordered by ``(projected utilization, sessions placed this pass, id)``.
    Every term is deterministic and the id breaks the last tie, so the same
    inputs always choose the same account no matter what order ccswap listed
    them in.
    """
    usable = [a for a in accounts if _blocker(a, policy, klass, placing=True) is None]
    if not usable:
        return None
    return min(
        usable,
        key=lambda a: (
            (binding_window(a, klass).utilization or 0.0)
            + NOTIONAL_SESSION_LOAD * placed.get(a.id, 0),
            placed.get(a.id, 0),
            a.id,
        ),
    )


def plan(
    projects: Sequence[Project],
    snapshot: AccountsSnapshot,
    policy: Policy,
    prior_map: Mapping[str, MapEntry],
    *,
    now: float,
) -> Plan:
    """Decide an account for every project. Never raises; never refuses.

    Routing can NEVER be the reason a bring-up fails: with routing off, with
    ccswap unreachable, or with no account able to take the work, every row
    comes back ``unrouted-*`` and the caller launches exactly as it does
    today. That is why there is no error return -- only rows that say why.

    Order of decisions, and each one's reason for existing:

    1. **Pins win, always**, even over the hard limit -- the user typed it. A
       pin naming an account ccswap does not report is a warning and the
       project is treated as unpinned, never silently re-routed.
    2. **Stickiness**: a project whose previous account is still usable keeps
       it (`kept`). Moving costs a measured ~10.5k-token cold write.
    3. **Moves are named by cause**: `moved-hard` when the old account is at
       or over the hard threshold, `moved-soft` for every other reason it can
       no longer host the project (excluded, ineligible, unhydrated, reserved
       for another class, no longer reported at all).
    4. **The rest are assigned** in the caller's order -- config order, the
       stable order everything else in this repo uses -- to the account with
       the most projected headroom.

    ``now`` is injected rather than read, so a plan is reproducible: it is used
    only to word "resets in ...".
    """
    if not policy.enabled:
        return Plan(rows=_unrouted_rows(projects, "unrouted-disabled", prior_map))
    stale = (
        snapshot.usage_age_s is not None and snapshot.usage_age_s > policy.stale_after_s
    )
    if snapshot.error or not snapshot.accounts:
        return Plan(
            rows=_unrouted_rows(projects, "unrouted-no-data", prior_map),
            stale=stale,
            usage_age_s=snapshot.usage_age_s,
            error=snapshot.error,
        )

    by_id = snapshot.by_id()
    placed: dict[str, int] = {}
    rows: list[Row] = []

    for project in projects:
        prior = prior_map.get(project.session)
        klass, source = _classify(project, prior)
        warning: str | None = None

        pinned = by_id.get(project.account) if project.account else None
        if project.account and pinned is None:
            warning = (
                f"pinned account {project.account!r} is not one ccswap reports; "
                "ignoring the pin"
            )
        if pinned is not None:
            blocker = _blocker(pinned, policy, klass, placing=False)
            over = blocker == _BLOCK_HARD
            reason = "pinned-over-limit" if over else "pinned"
            if blocker is not None:
                window = binding_window(pinned, klass)
                percent = _percent(window)
                detail = (
                    f"at {percent:.0f}% of its binding window"
                    if over and percent is not None
                    else blocker
                )
                resets = _until(window.resets_at, now)
                if resets:
                    detail = f"{detail}, resets {resets}"
                warning = f"account {pinned.id} is {detail}; pinned anyway"
            rows.append(_row_for(project, pinned, klass, source, reason, warning))
            placed[pinned.id] = placed.get(pinned.id, 0) + 1
            continue

        move_reason = "assigned"
        previous = by_id.get(prior.account) if prior else None
        if prior and previous is None:
            warning = warning or (
                f"account {prior.account!r} is no longer reported by ccswap"
            )
            move_reason = "moved-soft"
        elif previous is not None:
            blocker = _blocker(previous, policy, klass, placing=False)
            if blocker is None:
                rows.append(_row_for(project, previous, klass, source, "kept", None))
                placed[previous.id] = placed.get(previous.id, 0) + 1
                continue
            if blocker == _BLOCK_HARD:
                window = binding_window(previous, klass)
                percent = _percent(window)
                resets = _until(window.resets_at, now)
                move_reason = "moved-hard"
                warning = (
                    f"left account {previous.id}"
                    + (f" at {percent:.0f}%" if percent is not None else "")
                    + (f", resets {resets}" if resets else "")
                )
            else:
                move_reason = "moved-soft"
                warning = f"left account {previous.id}: {blocker}"

        chosen = _best_account(snapshot.accounts, policy, klass, placed)
        if chosen is None:
            rows.append(
                Row(
                    session=project.session,
                    project=project.name or project.session,
                    account=None,
                    klass=klass,
                    class_source=source,
                    reason="unrouted-no-eligible-account",
                    warning=warning,
                )
            )
            continue
        rows.append(_row_for(project, chosen, klass, source, move_reason, warning))
        placed[chosen.id] = placed.get(chosen.id, 0) + 1

    return Plan(
        rows=tuple(rows),
        stale=stale,
        usage_age_s=snapshot.usage_age_s,
        error=None,
    )
