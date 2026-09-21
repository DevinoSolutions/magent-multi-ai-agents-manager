"""``magent account`` -- see what the router sees, and pin what it must not guess.

Four subcommands, and exactly one of them writes anything:

- ``magent account`` (the default) prints the account table: what ccswap
  reports, every reason magent would refuse to route on, and where work is
  recorded today.
- ``magent account plan`` is ``--go``'s dry run. It builds the same snapshot,
  applies the same refusals and calls the same ``routing.plan``, so a preview
  that disagreed with a launch would have to be a bug in one function rather
  than a second implementation drifting from the first. It changes nothing on
  disk -- not the config, not the assignment map.
- ``magent account pin`` / ``unpin`` write the per-project pin -- user intent --
  through the raw-dict config seam (``cli/config_io``), so a hand-added or
  forward-compatible key in somebody's config survives the write.
- ``magent account refresh`` asks ccswap for fresher usage numbers. NEVER on a
  launch path: a bring-up must not block on somebody else's network read, and
  this is the interactive escape hatch for when the cached numbers are old.

``magent account move`` is deliberately absent. Moving a live session means
recreating it under a different ``CLAUDE_CONFIG_DIR``, which needs the launch
path's per-window env overlay; that lands in its own PR. The vocabulary it will
print already exists (``routing.REASONS``, ``accounts.INELIGIBLE_REASONS``).

Two properties this module holds on purpose:

- **It cannot change placement.** The pin is config, the assignment is machine
  state in ``~/.magent/account-map.json``, and nothing here writes the second.
- **No email reaches stdout.** ``accounts`` redacts ccswap's label at parse
  time, so there is no raw address in memory for a print site here to leak.
"""

from __future__ import annotations

import json
import sys
import time
from typing import TYPE_CHECKING

import click

from magent.cli.app import main
from magent.cli.config_io import (
    _load_config_or_exit,
    _load_raw_config,
    _save_raw_config,
)
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from magent import accounts as accounts_mod
    from magent import routing as routing_mod
    from magent.config import MagentConfig

# Exit codes, the same vocabulary `magent send` uses so the two command families
# read alike: 2 the thing you named does not exist, 3 ccswap could not be reached
# or would not answer. The 0/1/3 contract of `status`/`doctor` is untouched.
_EXIT_NOT_FOUND = 2
_EXIT_CCSWAP_ERROR = 3


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def routing_allowed() -> bool:
    """``MAGENT_ACCOUNT_ROUTING`` -- the product-wide kill switch.

    Delegates: the launch path needs the same answer and a src module may not
    import this package (LS-A-001), so the implementation lives in
    ``routing.py`` and both callers read it from there.
    """
    from magent import routing  # heavy subsystem: in-body per policy

    return routing.routing_allowed()


def policy_for(cfg: MagentConfig) -> routing_mod.Policy:
    """``settings.accounts`` as the planner's ``Policy``, kill switch included.

    One translation used by the table, the plan, `doctor`'s check and the
    bring-up alike, so none of them can disagree about whether routing is even
    on. It lives in ``routing.py`` for the same reason as ``routing_allowed``
    above; this is the config-shaped front door onto it.
    """
    from magent import routing  # heavy subsystem: in-body per policy

    return routing.policy_for(cfg.settings.accounts)


def _projects(cfg: MagentConfig) -> list[routing_mod.Project]:
    """The projects routing applies to, in config order.

    ``psmux.eligible_projects`` decides the set, so this command can never
    disagree with a bring-up about which projects exist: enabled, a CLI agent
    rather than an IDE, and LOCAL -- a ``host:`` project's command runs on the
    far machine, where magent sets no environment, so v1 does not route it.
    """
    from magent import psmux, routing  # heavy subsystem: in-body per policy

    typed_by_path = {p.path: p for p in cfg.projects}
    out: list[routing.Project] = []
    for descriptor in psmux.eligible_projects(cfg):
        project = typed_by_path.get(_as_str(descriptor.get("path")))
        out.append(
            routing.Project(
                session=psmux.socket_id(descriptor),
                name=_as_str(descriptor.get("name")),
                account=project.account if project else None,
                model_class=project.model_class if project else None,
            )
        )
    return out


# --- the refusals ------------------------------------------------------------


def _refusals(
    snapshot: accounts_mod.AccountsSnapshot,
    settings: accounts_mod.SettingsReport,
    version: str | None,
) -> list[str]:
    """Every reason magent will not route, in the order it checks them.

    A refusal is not a failure: the fleet always launches, unrouted. Each line
    names the condition and, where there is one, the exact command that clears
    it -- magent never flips somebody else's setting itself (the `wt-keys`
    posture, where the user's own configuration always wins).

    Version and settings come first because they invalidate the whole snapshot
    rather than any one account. ``duplicate_warnings`` is the sharpest of them:
    the SAME LOGIN present in more than one slot makes it ambiguous whose quota
    a reading describes, so the distrust is of the SNAPSHOT, not of an account,
    and routing on it would place work by somebody else's numbers. It is not
    about two different logins sharing an ORGANIZATION -- that is an ordinary
    setup which ccswap deliberately does not report, and wording that blurred
    the two would send people hunting a non-problem.
    """
    from magent import accounts  # heavy subsystem: in-body per policy

    out: list[str] = []
    if not accounts.version_at_least(version):
        out.append(
            f"ccswap {version or 'version unreadable'} is older than "
            f"{accounts.MIN_CCSWAP_VERSION}, the build that answers a read-only "
            "`list --profiles`; upgrade ccswap"
        )
    out.extend(settings.problems)
    if settings.error:
        out.append(f"{settings.error} -- magent will not read that as a yes")
    if snapshot.duplicate_warnings:
        out.append(
            "ccswap reports the same login in more than one slot, so a "
            "utilization reading may be attributed to the wrong account: "
            + "; ".join(snapshot.duplicate_warnings)
        )
    if snapshot.error:
        out.append(snapshot.error)
    return out


def _snapshot_for_plan(
    snapshot: accounts_mod.AccountsSnapshot, refusals: list[str]
) -> accounts_mod.AccountsSnapshot:
    """The snapshot the planner is allowed to see.

    A refusal invalidates the whole read, so it is handed to ``routing.plan`` as
    an ERROR snapshot rather than as a special reason code: every row then comes
    back ``unrouted-no-data`` out of the planner's closed vocabulary and the
    refusal text rides along on ``Plan.error``. No new reason, no second code
    path, and `plan` stays a truthful preview of what a launch would do.
    """
    from magent import accounts  # heavy subsystem: in-body per policy

    if not refusals:
        return snapshot
    return accounts.AccountsSnapshot(
        usage_age_s=snapshot.usage_age_s,
        duplicate_warnings=snapshot.duplicate_warnings,
        schema_version=snapshot.schema_version,
        error=refusals[0],
    )


def _read_everything(
    config_file: Path, *, as_json: bool = False
) -> tuple[
    MagentConfig,
    accounts_mod.AccountsSnapshot,
    accounts_mod.SettingsReport,
    list[str],
]:
    """One ccswap read per invocation, shared by every surface here.

    ``accounts`` serialises its own subprocesses; one cached snapshot per
    command is the other half of that rule. Two reads in one command could
    disagree, and a table whose rows came from different moments explains
    nothing.
    """
    from magent import accounts  # heavy subsystem: in-body per policy

    cfg = _load_config_or_exit(config_file, as_json=as_json)
    binary = accounts.find_ccswap()
    snapshot = accounts.read_accounts(ccswap=binary)
    settings = accounts.read_settings(ccswap=binary)
    version = accounts.read_version(ccswap=binary)
    return cfg, snapshot, settings, _refusals(snapshot, settings, version)


# --- rendering (ASCII only: these tables land in bug reports and log pastes) --


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _reset_text(resets_at: float | None, now: float) -> str:
    """``"5h 12m"`` / ``"2d 3h"`` / ``"-"``.

    Deliberately not ``routing._until``'s wording: that one narrates a warning
    sentence ("resets in 2h 13m"), this is a table cell and needs days, because
    a 7-day window's reset is routinely more than 24 hours out.
    """
    if resets_at is None:
        return "-"
    remaining = int(resets_at - now)
    if remaining <= 0:
        return "due"
    hours, minutes = remaining // 3600, remaining // 60 % 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _account_state(
    acct: accounts_mod.Account, policy: routing_mod.Policy
) -> tuple[str, str]:
    """``(state, colour)`` for one account's table row.

    Display only. The planner's own blocker answers a different question
    (placing NEW work vs. keeping what is already there), so a table that reused
    it would report "cannot take work" for an account happily hosting four
    sessions. A reason ccswap gave is printed in ITS words, never re-derived.
    """
    from magent import accounts, routing  # heavy subsystem: in-body per policy

    if acct.kind and acct.kind != accounts.SUBSCRIPTION_KIND:
        return f"{acct.kind} (not a subscription)", "yellow"
    if not acct.eligible:
        return acct.ineligible_text or "ineligible in ccswap", "yellow"
    if policy.account_policy(acct.id).exclude:
        return "excluded in settings", "yellow"
    if not acct.hydrated:
        return "profile holds no login", "yellow"
    window = routing.binding_window(acct, routing.CLASS_STANDARD)
    if window.utilization is None:
        return "no usage reading", "yellow"
    pct = window.utilization * 100.0
    if pct >= policy.hard_threshold:
        return f"hard ({pct:.0f}%)", "red"
    if pct >= policy.soft_threshold:
        return f"soft ({pct:.0f}%)", "yellow"
    return "ok", "green"


def _placement_counts(
    projects: list[routing_mod.Project], prior: Mapping[str, accounts_mod.MapEntry]
) -> dict[str, int]:
    """Per account, how many projects sit on it right now.

    A pin outranks the recorded assignment for the same project, because that is
    the order the planner resolves them in.
    """
    counts: dict[str, int] = {}
    for project in projects:
        entry = prior.get(project.session)
        acct = project.account or (entry.account if entry else None)
        if acct:
            counts[acct] = counts.get(acct, 0) + 1
    return counts


def routing_off_reason() -> str:
    """WHICH gate is holding routing off -- there are two, and they need
    different actions. Shared with the bring-up, so a `doctor` line and a
    launch line cannot name different causes; see ``routing.py``."""
    from magent import routing  # heavy subsystem: in-body per policy

    return routing.routing_off_reason()


def _note(text: str) -> None:
    click.echo(f"  {style('!', fg='yellow')} {text}")


def _print_notes(
    policy: routing_mod.Policy,
    snapshot: accounts_mod.AccountsSnapshot,
    refusals: list[str],
) -> None:
    if not policy.enabled:
        _note(routing_off_reason())
    if snapshot.usage_age_s is not None and snapshot.usage_age_s > policy.stale_after_s:
        _note(
            f"ccswap usage data is {snapshot.usage_age_s / 60:.0f}m old -- refresh "
            "it with `magent account refresh`"
        )
    for line in refusals:
        _note(line)


_ACCOUNT_HEADERS = ("account", "class", "5h", "7d", "fable", "resets", "proj", "state")
_ACCOUNT_WIDTHS = (8, 9, 6, 6, 6, 9, 5, 0)
_PLAN_HEADERS = ("project", "account", "class", "source", "util", "resets", "reason")
_PLAN_WIDTHS = (0, 8, 9, 9, 6, 9, 0)


def _row(cells: list[str], widths: tuple[int, ...]) -> str:
    """One table line. A width of 0 means "last column, do not pad" -- which is
    also the only column allowed to carry colour, since padding a styled string
    pads its escape sequences too."""
    return (
        "  "
        + " ".join(
            cell if width == 0 else cell.ljust(width)
            for cell, width in zip(cells, widths, strict=True)
        ).rstrip()
    )


def _print_account_table(config_file: Path) -> None:
    from magent import accounts, routing  # heavy subsystem: in-body per policy

    cfg, snapshot, _settings, refusals = _read_everything(config_file)
    policy = policy_for(cfg)
    projects = _projects(cfg)
    prior = accounts.read_map()
    counts = _placement_counts(projects, prior)
    now = time.time()

    click.echo()
    click.echo(f"  {style('magent accounts', bold=True)}")
    click.echo()
    if not snapshot.accounts:
        _note(snapshot.error or "ccswap reported no claude accounts")
        _print_notes(policy, snapshot, [r for r in refusals if r != snapshot.error])
        return

    click.echo(style(_row(list(_ACCOUNT_HEADERS), _ACCOUNT_WIDTHS), bold=True))
    for acct in snapshot.accounts:
        per = policy.account_policy(acct.id)
        state, colour = _account_state(acct, policy)
        fable = acct.scoped.get(routing.CLASS_FABLE)
        binding = routing.binding_window(acct, routing.CLASS_STANDARD)
        click.echo(
            _row(
                [
                    acct.id,
                    f"{per.klass}*" if per.klass else "-",
                    _percent(acct.five_hour.utilization),
                    _percent(acct.seven_day.utilization),
                    _percent(fable.utilization if fable else None),
                    _reset_text(binding.resets_at, now),
                    str(counts.get(acct.id, 0)),
                    style(state, fg=colour),
                ],
                _ACCOUNT_WIDTHS,
            )
        )
    click.echo()
    if any(policy.account_policy(a.id).klass for a in snapshot.accounts):
        click.echo(
            f"  {style('* class reserved in settings.accounts.perAccount', dim=True)}"
        )
    _print_notes(policy, snapshot, refusals)
    _print_placements(projects, prior)


def _print_placements(
    projects: list[routing_mod.Project], prior: Mapping[str, accounts_mod.MapEntry]
) -> None:
    """What is recorded for each project today -- a read of the map, not a plan.

    Pins are labelled as pins: the whole reason the assignment lives outside the
    config is that a pin and a guess must never be confused on disk, and
    printing them identically here would undo that.
    """
    rows: list[tuple[str, str, str, str]] = []
    for project in projects:
        entry = prior.get(project.session)
        account = project.account or (entry.account if entry else "")
        if not account:
            continue
        source = "pin" if project.account else "map"
        detail = f"{entry.klass} {entry.class_source}" if entry else ""
        rows.append((project.name or project.session, account, source, detail))
    if not rows:
        return
    width = max(len(r[0]) for r in rows)
    click.echo()
    click.echo(f"  {style('recorded placements', bold=True)}")
    click.echo()
    for name, account, source, detail in rows:
        # Padded only when something follows it: `style` always emits its escape
        # codes (echo strips them off a tty), so a trailing `.rstrip()` would
        # see the reset sequence rather than the padding and leave it behind.
        line = f"  {name.ljust(width)}  {account.ljust(6)} "
        if detail:
            line += f"{style(source.ljust(4), dim=True)} {style(detail, dim=True)}"
        else:
            line += style(source, dim=True)
        click.echo(line)


def _plan_json(
    plan: routing_mod.Plan, policy: routing_mod.Policy, refusals: list[str]
) -> dict[str, object]:
    from magent import routing  # heavy subsystem: in-body per policy

    return {
        "ok": True,
        "enabled": policy.enabled,
        "refusals": refusals,
        "stale": plan.stale,
        "usageAgeS": plan.usage_age_s,
        "error": plan.error,
        "rows": [
            {
                "session": row.session,
                "project": row.project,
                "account": row.account,
                "class": row.klass,
                "classSource": row.class_source,
                "reason": row.reason,
                "reasonText": routing.REASONS.get(row.reason, ""),
                "utilization": row.utilization,
                "resetsAt": row.resets_at,
                "warning": row.warning,
            }
            for row in plan.rows
        ],
    }


def _print_plan(
    plan: routing_mod.Plan,
    policy: routing_mod.Policy,
    snapshot: accounts_mod.AccountsSnapshot,
    refusals: list[str],
) -> None:
    click.echo()
    click.echo(
        f"  {style('magent account plan', bold=True)} "
        f"{style('(a dry run -- nothing is changed)', dim=True)}"
    )
    click.echo()
    if not plan.rows:
        _note("no local CLI-agent projects in this config -- nothing to route")
        _print_notes(policy, snapshot, refusals)
        return

    widths = (max(len(row.project) for row in plan.rows), *_PLAN_WIDTHS[1:])
    click.echo(style(_row(list(_PLAN_HEADERS), widths), bold=True))
    now = time.time()
    for row in plan.rows:
        unrouted = row.reason.startswith("unrouted")
        click.echo(
            _row(
                [
                    row.project,
                    row.account or "-",
                    row.klass,
                    row.class_source,
                    _percent(row.utilization),
                    _reset_text(row.resets_at, now),
                    style(row.reason, fg="yellow") if unrouted else row.reason,
                ],
                widths,
            )
        )
    click.echo()
    for row in plan.rows:
        if row.warning:
            _note(f"{row.project}: {row.warning}")
    _print_notes(policy, snapshot, refusals)


# --- name resolution ---------------------------------------------------------


def _resolve_project(
    query: str, projects: list[routing_mod.Project]
) -> routing_mod.Project | None:
    """Resolve a project the way `send`/`peek` resolve a session: exact, then a
    unique substring, then a unique prefix -- so one string works everywhere.
    Matched against the session id first and the project name second, because a
    pin is about a project and the user may reasonably type either."""
    from magent import fleet  # heavy subsystem: in-body per policy

    by_session = {p.session: p for p in projects}
    match = fleet.resolve_session(query, list(by_session))
    if match:
        return by_session[match]
    by_name = {p.name: p for p in projects if p.name}
    match = fleet.resolve_session(query, list(by_name))
    return by_name[match] if match else None


def _project_or_exit(
    query: str, projects: list[routing_mod.Project]
) -> routing_mod.Project:
    project = _resolve_project(query, projects)
    if project:
        return project
    click.echo(
        f"  {style('x', fg='red')} no configured project matches '{query}'.", err=True
    )
    known = ", ".join(p.session for p in projects[:12])
    if known:
        click.echo(f"  {style('projects:', dim=True)} {known}", err=True)
    sys.exit(_EXIT_NOT_FOUND)


def _write_pin(
    config_file: Path, project: routing_mod.Project, account: str | None
) -> None:
    """Set or clear ``projects[i].account`` through the round-tripping seam.

    ``_load_raw_config``/``_save_raw_config`` preserve every key magent does not
    model -- the whole reason that second config path exists, and exactly what a
    pin needs, since a user's file may already carry fields a newer magent adds.

    The entry is found by recomputing its session id the way
    ``psmux.eligible_projects`` does, so the name that resolved the project is
    the name that identifies the row to write.
    """
    from magent import psmux  # heavy subsystem: in-body per policy
    from magent.titles import get_leaf_name

    data = _load_raw_config(config_file)
    for entry in _as_list(data.get("projects")):
        if not isinstance(entry, dict):
            continue
        path = _as_str(entry.get("path"))
        if not path:
            continue
        leaf = _as_str(entry.get("title")) or get_leaf_name(path)
        if psmux.session_name(leaf) != project.session:
            continue
        # `entry` is the dict inside `data`, so mutating it here is what the
        # save persists.
        if account is None:
            entry.pop("account", None)
        else:
            entry["account"] = account
    _save_raw_config(config_file, data)


# --- the commands ------------------------------------------------------------


@main.group("account", invoke_without_command=True)
@click.pass_context
def account_group(ctx: click.Context) -> None:
    """Which Claude account each project runs on.

    With no subcommand: the account table -- what ccswap reports for every
    account, how much of each one's quota is spent, and how many projects sit on
    it. Reads only. The one command here that writes is `pin`/`unpin`, and it
    writes the config, never a running session.
    """
    if ctx.invoked_subcommand is None:
        _print_account_table(find_config(ctx.obj.get("config_path")))


@account_group.command("plan")
@click.option("--json", "as_json", is_flag=True, help="Print the plan as JSON.")
@click.pass_context
def account_plan(ctx: click.Context, as_json: bool) -> None:
    """Show which account each project WOULD get. Changes nothing.

    The same snapshot, the same refusals and the same planner a launch uses, so
    this is a dry run rather than a second opinion. Every row carries a reason
    from the planner's closed vocabulary.
    """
    from magent import accounts, routing  # heavy subsystem: in-body per policy

    config_file = find_config(ctx.obj.get("config_path"))
    cfg, snapshot, _settings, refusals = _read_everything(config_file, as_json=as_json)
    policy = policy_for(cfg)
    plan = routing.plan(
        _projects(cfg),
        _snapshot_for_plan(snapshot, refusals),
        policy,
        accounts.read_map(),
        now=time.time(),
    )
    if as_json:
        click.echo(json.dumps(_plan_json(plan, policy, refusals), indent=2))
        return
    _print_plan(plan, policy, snapshot, refusals)


@account_group.command("pin")
@click.argument("project")
@click.argument("account")
@click.pass_context
def account_pin(ctx: click.Context, project: str, account: str) -> None:
    """Pin PROJECT to ACCOUNT in the config -- user intent, never overridden.

    A pin wins over every threshold: the planner honours it even on an account
    that is over the hard limit, and says so rather than quietly re-routing.
    ACCOUNT is a ccswap account id (see `magent account`) and is not validated
    here, so pinning never depends on ccswap being reachable -- an id ccswap
    does not report is reported by `magent account plan`, with the pin ignored.
    """
    config_file = find_config(ctx.obj.get("config_path"))
    target = _project_or_exit(project, _projects(_load_config_or_exit(config_file)))
    _write_pin(config_file, target, account.strip())
    click.echo(
        f"  {style('OK', fg='green')} {style(target.name or target.session, bold=True)}"
        f" is pinned to account {style(account.strip(), bold=True)}"
    )
    click.echo(
        f"  {style('Run', dim=True)} {style('magent account plan', bold=True)} "
        f"{style('to see what it changes.', dim=True)}"
    )


@account_group.command("unpin")
@click.argument("project")
@click.pass_context
def account_unpin(ctx: click.Context, project: str) -> None:
    """Remove PROJECT's account pin, letting the planner place it again."""
    config_file = find_config(ctx.obj.get("config_path"))
    target = _project_or_exit(project, _projects(_load_config_or_exit(config_file)))
    _write_pin(config_file, target, None)
    click.echo(
        f"  {style('OK', fg='green')} {style(target.name or target.session, bold=True)}"
        " is no longer pinned"
    )


@account_group.command("refresh")
@click.pass_context
def account_refresh(ctx: click.Context) -> None:
    """Ask ccswap to re-read usage older than settings.accounts.staleAfterS.

    Interactive only. A bring-up never calls this: it must not block on somebody
    else's network read, and launch-time placement is exactly the use ccswap's
    own cache is adequate for.
    """
    from magent import accounts  # heavy subsystem: in-body per policy

    config_file = find_config(ctx.obj.get("config_path"))
    policy = policy_for(_load_config_or_exit(config_file))
    if not accounts.find_ccswap():
        click.echo(
            f"  {style('x', fg='red')} ccswap is not on PATH -- nothing to refresh.",
            err=True,
        )
        sys.exit(_EXIT_CCSWAP_ERROR)
    if not accounts.refresh_usage(policy.stale_after_s):
        click.echo(
            f"  {style('x', fg='red')} ccswap would not refresh its usage data.",
            err=True,
        )
        sys.exit(_EXIT_CCSWAP_ERROR)
    age = accounts.read_accounts().usage_age_s
    shown = "unknown age" if age is None else f"{age / 60:.0f}m old"
    click.echo(f"  {style('OK', fg='green')} ccswap usage data refreshed ({shown})")
