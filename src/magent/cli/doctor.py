"""`magent doctor` — environment diagnosis as a checklist.

`status` covers *daemons*; doctor covers *environment*: is the config
loadable and current, does the env validate, are the agent CLIs and a
terminal on PATH, can anything tile (monitors), are the runtime dirs
writable, is Tailscale reachable, is the upload port sane. Every check is
a small function returning (status, detail) so each is unit-testable; the
command is just the runner. Exit 0 = no failures (warns allowed), 1 = any
check failed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING

import click

from magent import log, psmux, tailnet
from magent.cli.app import main
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from magent import accounts as accounts_mod
    from magent.config import MagentConfig

OK = "ok"
WARN = "warn"
FAIL = "fail"

CheckResult = tuple[str, str]


def _check_config(config_file: Path) -> tuple[CheckResult, MagentConfig | None]:
    from magent.config import (  # heavy subsystem: in-body per policy
        SCHEMA_VERSION,
        ConfigError,
        load_config,
    )

    if not config_file.exists():
        return (FAIL, "no config found — run `magent --init`"), None
    try:
        cfg = load_config(str(config_file))
    except (ConfigError, FileNotFoundError) as exc:
        return (FAIL, f"config invalid: {exc}"), None
    if cfg.version < SCHEMA_VERSION:
        return (
            WARN,
            f"schema v{cfg.version} < v{SCHEMA_VERSION} — run `magent config migrate`",
        ), cfg
    return (OK, f"{len(cfg.projects)} project(s), schema v{cfg.version}"), cfg


def _check_env() -> CheckResult:
    from pydantic import ValidationError  # heavy subsystem: in-body per policy

    from magent import env as env_module  # heavy subsystem: in-body per policy

    try:
        env_module.get_env()
    except ValidationError as exc:
        names = ", ".join(
            name or msg for name, msg in env_module.validation_error_items(exc)
        )
        return (FAIL, f"invalid environment variable(s): {names} (see .env.example)")
    return (OK, "MAGENT_* environment validates")


def _check_agent_tools(cfg: MagentConfig | None) -> CheckResult:
    from magent.config import DEFAULT_TOOLS  # heavy subsystem: in-body per policy

    if cfg is None:
        tools = dict(DEFAULT_TOOLS)
        used = set(tools)
    else:
        tools = dict(cfg.settings.tools)
        used = {p.tool or cfg.settings.default_tool for p in cfg.projects if p.enabled}
    missing = sorted(
        name
        for name, cmd in tools.items()
        if name in used and cmd.split() and shutil.which(cmd.split()[0]) is None
    )
    if missing:
        return (WARN, f"tool command(s) not on PATH: {', '.join(missing)}")
    return (OK, "every configured agent tool resolves on PATH")


def _check_terminal() -> CheckResult:
    from magent.platform import (  # heavy subsystem: in-body per policy
        WT_INSTALL_HINT,
        find_psmux,
        get_platform,
    )

    if sys.platform == "win32":
        wt = shutil.which("wt")
        psmux = find_psmux()
        if not wt:
            return (
                FAIL,
                (
                    "Windows Terminal (wt) not on PATH — nothing can launch. "
                    f"Install: {WT_INSTALL_HINT}"
                ),
            )
        if get_platform().supports_psmux() and not psmux:
            return (WARN, "psmux not found — `up`/`attach` sessions unavailable")
        return (OK, "wt found" + (", psmux found" if psmux else ""))
    candidates = ("gnome-terminal", "konsole", "xterm", "alacritty", "kitty", "iTerm")
    found = [c for c in candidates if shutil.which(c)]
    if not found:
        return (WARN, "no known terminal emulator on PATH")
    return (OK, f"terminal: {found[0]}")


# The three facts an operator needs at 2am, in the order they need them. ASCII
# only: this lands in a psmux status line and in bug reports pasted anywhere.
WEDGE_REPAIR_HINT = (
    "The sessions behind it are FROZEN, not dead -- do NOT restart them, do "
    "NOT reboot; both destroy live agents that would otherwise come back.\n"
    "Recovery: find the conhost.exe processes whose parent chain reaches a dead "
    "pid or a psmux.exe, and kill ONLY those (measured: 14 of 874 conhosts).\n"
    "psmux answers again immediately after that (a hung new-session went to "
    "892 ms) and every session returns intact."
)


def _resident_psmux() -> str:
    """`` (N psmux.exe resident)``, or nothing at all.

    Enrichment only, and strictly optional: the count corroborates the wedge
    (the incident left psmux.exe processes that ignored ``taskkill /F``) but the
    repair does not depend on it, so an unknown count says nothing rather than
    guessing zero. ``count_processes`` is a Toolhelp snapshot -- single-digit
    milliseconds, no subprocess -- and answers None off Windows, so this can
    never add measurable time to a doctor run.
    """
    from magent.procs import count_processes

    found = count_processes("psmux.exe")
    return f" ({found} psmux.exe resident)" if found else ""


def _check_psmux_wedge() -> CheckResult:
    """Is the psmux CONTROL PLANE answering, or is the machine wedged?

    The failure this exists to name took hours to diagnose live: every psmux
    command -- has-session, list-sessions, new-session -- hung forever from any
    console, while ConPTY itself was healthy (a raw pywinpty spawn was
    instant). The whole fleet looked dead. It was not: after the wedge was
    cleared every session probed alive, so the expensive mistake available at
    that moment was mass-restarting 40 live agents.

    One bounded probe, and it is deliberately not a liveness sweep -- see
    ``psmux.probe_control_plane``.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_psmux():
        return (OK, "psmux not used on this OS (Windows-only feature)")
    if not psmux.find_psmux():
        return (OK, "skipped -- psmux not installed (see the terminal check)")

    probe = psmux.probe_control_plane()
    if probe.timed_out:
        return (
            FAIL,
            (
                f"psmux answered nothing in {probe.elapsed_s:.0f}s"
                f"{_resident_psmux()}: the control plane is WEDGED machine-wide.\n"
                f"{WEDGE_REPAIR_HINT}"
            ),
        )
    if not probe.responsive:
        return (WARN, "psmux is installed but would not run (see the terminal check)")
    return (OK, f"psmux control plane responded in {probe.elapsed_s:.2f}s")


def _check_psmux_session0() -> CheckResult:
    """Is anything of ours stranded in the logon session nobody can see?

    A psmux server in Session 0 is worse than a dead one: it answers
    ``has-session`` for its own socket (the registry under ``~/.psmux`` is
    shared across sessions), so it HOLDS the name while being invisible to the
    desktop's windows, unattachable from it, and — since Windows OpenSSH hands
    admins a full token — usually above the desktop user's integrity level too.
    That is the whole shape of the incident: every desktop bring-up logged
    "session never came up after respawn" for names a Session-0 server owned.

    WARN, never FAIL: magent did not start these (the hand-off exists so it
    never will again) and cannot stop them, so this must not start failing a
    doctor run on a machine whose only problem is that somebody once ssh'd in.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_psmux():
        return (OK, "psmux not used on this OS (Windows-only feature)")
    stranded = psmux.session0_server_pids()
    if not stranded:
        return (OK, "no psmux server runs in logon Session 0")
    return (WARN, psmux.session0_message(len(stranded)))


def _check_monitors() -> CheckResult:
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    monitors = get_platform().list_monitors()
    if not monitors:
        return (FAIL, "no monitors detected — tiling cannot place anything")
    return (OK, f"{len(monitors)} monitor(s) detected")


def _monitor_topology() -> list[dict[str, object]]:
    """The live monitor topology as plain dicts (``grid.MonitorRect`` fields),
    for the doctor --json ``monitors`` key. Never raises: a platform/DPI probe
    failure degrades to an empty list so doctor always produces a report.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    try:
        monitors = get_platform().list_monitors()
    except OSError:
        return []
    return [
        {
            "x": m.x,
            "y": m.y,
            "w": m.w,
            "h": m.h,
            "is_primary": m.is_primary,
            "scale_factor": round(m.scale_factor, 4),
        }
        for m in monitors
    ]


def _monitor_lines(monitors: list[dict[str, object]]) -> list[str]:
    """Terse one-line-per-monitor geometry for the human (non-JSON) report."""
    lines: list[str] = []
    for m in monitors:
        x, y, w, h = m["x"], m["y"], m["w"], m["h"]
        scale = m["scale_factor"]
        pct = round(scale * 100) if isinstance(scale, (int, float)) else scale
        tag = " *primary" if m["is_primary"] else ""
        lines.append(f"{w}x{h} @ ({x},{y}) {pct}%{tag}")
    return lines


def _check_hotkey(cfg: MagentConfig | None) -> CheckResult:
    """Is Alt+V actually working, not merely available.

    The old version answered "does this OS support the hotkey", which is true on
    every Windows box whether or not a listener has run since the last reboot --
    so a machine where Alt+V had been dead for days passed this check. It now
    reports the real listener liveness, through the same state machine `status`
    renders (``cli.status._listener_state``) so the two surfaces can never
    disagree about whether Alt+V works.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_hotkey():
        return (OK, "hotkey not supported on this OS (Windows-only feature)")

    from magent.cli.status import (
        LISTENER_REPAIR_HINT,
        _listener_state,
        _upload_state,
    )

    port = cfg.settings.upload_port if cfg else 8033
    state = _listener_state(_upload_state(port))
    if state == "on":
        return (OK, "Alt+V listener running (heartbeat fresh)")
    if state == "dead":
        return (
            FAIL,
            (
                "upload server is running but no Alt+V listener — pasting an image "
                f"into a magent: window does nothing. Repair: {LISTENER_REPAIR_HINT}"
            ),
        )
    if state == "stale":
        return (
            FAIL,
            (
                "Alt+V listener process is alive but its heartbeat expired — its "
                "message loop is wedged and key presses are being dropped. "
                f"Repair: {LISTENER_REPAIR_HINT}"
            ),
        )
    from magent.upload_server import (
        supervision_enabled,  # heavy subsystem: in-body per policy
    )

    if not supervision_enabled():
        return (OK, "Alt+V listener off — supervision disabled (you own its lifetime)")
    return (OK, "Alt+V listener off — it starts with the upload server")


def _check_wt_keys() -> CheckResult:
    """Do Ctrl+Backspace and Shift+Enter survive psmux?

    Never a FAIL, deliberately: a missing binding costs the user a word-delete
    and a soft newline, not a working fleet, and doctor's exit code is what CI
    and `magent status` read. Everything here is WARN-at-worst so the finding
    is loud without turning an ergonomic gap into a red machine.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_wt_keybindings():
        return (OK, "Windows Terminal keys not applicable on this OS (Windows-only)")

    from magent import wt_keys
    from magent.cli.terminal_cmd import REPAIR_HINT

    path = wt_keys.find_settings()
    if path is None:
        return (OK, "Windows Terminal settings.json not found -- nothing to configure")
    try:
        doc = wt_keys.load_settings(path)
    except (wt_keys.SettingsParseError, OSError) as exc:
        return (
            WARN,
            (
                f"cannot read {path}: {exc} -- run `{REPAIR_HINT}` for the snippet "
                "to paste by hand"
            ),
        )
    states = wt_keys.states(doc)
    conflicts = [s.keys for s in states if s.state == wt_keys.CONFLICT]
    missing = [s.keys for s in states if s.state == wt_keys.MISSING]
    if not conflicts and not missing:
        return (OK, "Ctrl+Backspace and Shift+Enter survive psmux")
    parts = []
    if missing:
        parts.append(f"not bound: {', '.join(missing)}")
    if conflicts:
        parts.append(f"bound to something else: {', '.join(conflicts)}")
    return (
        WARN,
        (
            f"{'; '.join(parts)} -- psmux eats the modifier, so these do nothing "
            f"in a pane. Repair: {REPAIR_HINT}"
        ),
    )


def _unusable_accounts(snapshot: accounts_mod.AccountsSnapshot) -> list[str]:
    """``"14: needs a fresh login"`` per account that cannot host work.

    Reported on the OK path as well as the WARN one, because this is the
    early-warning surface for the hazard the feature lives with: ccswap's
    store-to-profile write-back is skipped while a live session pid exists, and a
    resident routed fleet IS a permanently live pid on every account it uses --
    so a slot on its way to `invalid_grant` shows up here, with ccswap's own
    reason, before it costs a re-login. A reason ccswap gave is never re-derived.
    """
    from magent.accounts import SUBSCRIPTION_KIND

    out: list[str] = []
    for acct in snapshot.accounts:
        if acct.kind and acct.kind != SUBSCRIPTION_KIND:
            continue  # an api-key slot is not a routing target and never was
        if not acct.eligible:
            out.append(f"{acct.id}: {acct.ineligible_text or 'ineligible in ccswap'}")
        elif not acct.hydrated:
            out.append(f"{acct.id}: its profile holds no usable login")
    return out


# The ccswap settings magent must be able to VERIFY before it routes, with the
# value each one needs. `accounts.REQUIRED_SETTINGS` is the authority on what is
# actually read; this names them again for one purpose only -- to notice a key
# that was never ASKED about, because a key added to the contract after this
# magent shipped is simply absent from the report, and absence there reads as
# "verified" when it means "not verified". Delete an entry here only when it
# leaves the contract, and if a name ever disagrees with `accounts`' spelling
# the check says so out loud rather than going quiet.
_WANTED_CCSWAP_SETTINGS: tuple[tuple[str, str], ...] = (
    ("profiles.persistent", "true"),
    ("autoswitch.enabled", "false"),
    ("autoswitch.warmupFiveHour", "false"),
)


def _unverified_settings(report: accounts_mod.SettingsReport) -> str:
    """The required ccswap settings this build never asked about, or ``""``.

    Distinct from a setting that read the wrong value (that is a `problem`) and
    from one that could not be read (that is the report's `error`): this is the
    third state, where nobody looked. It is named rather than assumed, because
    an unverified `autoswitch.warmupFiveHour` spends the very headroom the
    planner just budgeted, silently.
    """
    missing = [
        f"{key} (magent needs {wanted})"
        for key, wanted in _WANTED_CCSWAP_SETTINGS
        if key not in report.values
    ]
    if not missing:
        return ""
    return (
        f"not verified by this build: {', '.join(missing)} -- upgrade magent, or "
        "check by hand with `ccswap config get <key>`"
    )


def _duplicate_login_warning(warnings: tuple[str, ...]) -> str:
    """The duplicate-slot refusal, worded as the hazard it actually is.

    ccswap reports this when the SAME LOGIN is present in more than one slot,
    which makes it ambiguous whose quota a utilization reading describes -- so
    magent refuses to place work by numbers that may belong to another account.
    It is emphatically NOT about two different logins sharing an organization:
    that is a perfectly ordinary setup, and ccswap deliberately does not report
    it. Wording that blurred the two would send people hunting a non-problem.
    """
    return (
        "ccswap reports the same login in more than one slot, so a utilization "
        "reading may be attributed to the wrong account -- magent will not route "
        "on it: " + "; ".join(warnings)
    )


def _check_account_routing(cfg: MagentConfig | None) -> CheckResult:
    """Can per-project account routing work -- and is any slot drifting?

    WARN-at-worst, deliberately, on the `wt-keys` precedent: every condition
    here degrades to "the fleet launches unrouted", which is today's behaviour,
    so none of it may move doctor's exit code (which CI and `magent status`
    read). It also never MUTATES: every ccswap command it runs is a read, and
    the whole check is skipped while routing is off -- which is the default, so
    on an ordinary machine this costs no subprocess at all.
    """
    from magent import accounts  # heavy subsystem: in-body per policy
    from magent.cli.account_cmd import policy_for

    if cfg is None:
        return (OK, "skipped -- the config did not load (see the config check)")
    if not policy_for(cfg).enabled:
        return (OK, "account routing is off (settings.accounts.enabled)")

    binary = accounts.find_ccswap()
    if not binary:
        return (
            WARN,
            (
                "routing is on but ccswap is not on PATH, so every project "
                "launches unrouted -- install ccswap or turn routing off"
            ),
        )
    version = accounts.read_version(ccswap=binary)
    if not accounts.version_at_least(version):
        return (
            WARN,
            (
                f"ccswap {version or 'version unreadable'} is older than "
                f"{accounts.MIN_CCSWAP_VERSION}, the build with a read-only "
                "`list --profiles`; magent will not route until it is upgraded"
            ),
        )
    settings = accounts.read_settings(ccswap=binary)
    # Carried by every verdict from here on, including the OK one: a setting
    # nobody read is a gap in what this check PROVED, and hiding it behind a
    # louder finding is how it would stay unnoticed.
    unverified = _unverified_settings(settings)
    notes = f"\n{unverified}" if unverified else ""
    if settings.problems:
        return (WARN, "; ".join(settings.problems) + notes)
    if settings.error:
        return (WARN, f"{settings.error} -- magent will not read that as a yes{notes}")

    snapshot = accounts.read_accounts(ccswap=binary)
    if snapshot.error:
        return (WARN, snapshot.error + notes)
    if snapshot.duplicate_warnings:
        return (WARN, _duplicate_login_warning(snapshot.duplicate_warnings) + notes)
    usable = [
        a
        for a in snapshot.accounts
        if a.eligible and a.hydrated and a.kind == accounts.SUBSCRIPTION_KIND
    ]
    age = (
        ""
        if snapshot.usage_age_s is None
        else f", usage data {snapshot.usage_age_s / 60:.0f}m old"
    )
    unusable = _unusable_accounts(snapshot)
    tail = ("\n" + "; ".join(unusable)) if unusable else ""
    if not usable:
        return (
            WARN,
            (
                f"{len(snapshot.accounts)} ccswap account(s), none of them both "
                f"eligible and hydrated -- nothing to route to{age}{tail}{notes}"
            ),
        )
    return (
        OK,
        (
            f"{len(usable)}/{len(snapshot.accounts)} ccswap account(s) can host "
            f"work{age}{tail}{notes}"
        ),
    )


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d, prefix=".doctor-", delete=True):
            pass
    except OSError:
        return False
    return True


def _check_logs_dir() -> CheckResult:
    # log.LOG_DIR attribute access (not a by-value import) so tests'
    # monkeypatched isolation dir is honored.
    if _writable(log.LOG_DIR):
        return (OK, f"logs writable: {log.LOG_DIR}")
    return (FAIL, f"cannot write logs under {log.LOG_DIR}")


def _check_state_dir() -> CheckResult:
    from magent import agent_state  # heavy subsystem: in-body per policy

    if _writable(agent_state.STATE_DIR):
        return (OK, f"agent-state store writable: {agent_state.STATE_DIR}")
    return (
        FAIL,
        f"cannot write {agent_state.STATE_DIR} — agent hooks can't record states",
    )


def _check_sentry() -> CheckResult:
    """Surface the DSN-set-but-SDK-missing state HERE, not at CLI entry:
    init_sentry degrades to a log-file warning so everyday commands stay
    quiet, and doctor is where the actionable hint lives. sentry-sdk is a
    base dependency, so the WARN below indicates a broken install, not a
    missing optional extra."""
    from pydantic import ValidationError  # heavy subsystem: in-body per policy

    from magent import env as env_module  # heavy subsystem: in-body per policy
    from magent.sentry import SENTRY_INSTALL_HINT, sdk_installed

    try:
        dsn = env_module.get_env().sentry_dsn
    except ValidationError:
        return (OK, "skipped — environment invalid (see the env check)")
    if dsn is None:
        return (OK, "error reporting off (MAGENT_SENTRY_DSN not set)")
    if not sdk_installed():
        return (
            WARN,
            (
                "MAGENT_SENTRY_DSN is set but sentry-sdk is missing — error "
                "reporting is OFF. sentry-sdk ships with magent, so this "
                f"install looks broken. Repair: {SENTRY_INSTALL_HINT}"
            ),
        )
    return (OK, "error reporting active (DSN set, sentry-sdk installed)")


def _check_tailscale() -> CheckResult:
    p = tailnet.probe()
    if not p.on_path:
        return (WARN, "tailscale not on PATH — upload server binds loopback only")
    if not p.responding:
        return (WARN, "tailscale present but not responding")
    if p.ip:
        return (OK, f"tailscale up ({p.ip})")
    return (WARN, "tailscale installed but no IPv4 (logged out or down?)")


def _check_upload_port(cfg: MagentConfig | None) -> CheckResult:
    from magent.cli.background import (
        _probe_port,
        _running_upload_port,
    )

    port = cfg.settings.upload_port if cfg else 8033
    running = _running_upload_port()
    if running == port:
        return (OK, f"upload server already running on {port}")
    if _probe_port(port):
        return (WARN, f"port {port} is occupied by something else")
    return (OK, f"port {port} is free")


def _run_checks(config_file: Path) -> list[dict[str, str]]:
    (config_res, cfg) = _check_config(config_file)
    checks: list[tuple[str, CheckResult]] = [("config", config_res)]
    rest: list[tuple[str, Callable[[], CheckResult]]] = [
        ("env", _check_env),
        ("agent tools", lambda: _check_agent_tools(cfg)),
        ("terminal", _check_terminal),
        ("psmux wedge", _check_psmux_wedge),
        ("psmux-session0", _check_psmux_session0),
        ("monitors", _check_monitors),
        ("hotkey", lambda: _check_hotkey(cfg)),
        ("wt-keys", _check_wt_keys),
        ("account-routing", lambda: _check_account_routing(cfg)),
        ("logs dir", _check_logs_dir),
        ("state dir", _check_state_dir),
        ("sentry", _check_sentry),
        ("tailscale", _check_tailscale),
        ("upload port", lambda: _check_upload_port(cfg)),
    ]
    checks.extend((name, fn()) for name, fn in rest)
    return [
        {"name": name, "status": status, "detail": detail}
        for name, (status, detail) in checks
    ]


_MARKS = {
    OK: ("+", "green"),
    WARN: ("!", "yellow"),
    FAIL: ("x", "red"),
}


@main.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Print check results as JSON")
@click.pass_context
def doctor_cmd(ctx: click.Context, as_json: bool) -> None:
    """Diagnose the environment: config, env vars, tools, display, dirs.

    One line per check with an actionable hint on warn/fail. Exit 0 when
    nothing failed (warnings allowed), 1 when any check failed.
    """
    config_file = find_config(ctx.obj.get("config_path"))
    results = _run_checks(config_file)
    failures = sum(1 for r in results if r["status"] == FAIL)
    monitors = _monitor_topology()

    if as_json:
        # P3-04: `ok: true` -- doctor always produces a valid report; the
        # per-check result lives in `failures` (and the exit code). `monitors`
        # is additive: the exact topology a bug report can replay in the
        # monitor-lab tier (see tests/platform/doctor_replay.py).
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "checks": results,
                    "failures": failures,
                    "monitors": monitors,
                }
            )
        )
        sys.exit(1 if failures else 0)

    click.echo(f"  {style('magent doctor', bold=True)}")
    click.echo()
    for r in results:
        mark, color = _MARKS[r["status"]]
        dim = r["status"] == OK
        # A detail may be several lines (a repair runbook, not a sentence);
        # continuation lines are indented under the first so the checklist
        # column survives.
        first, *rest = r["detail"].split("\n")
        click.echo(
            f"  {style(mark, fg=color, bold=True)} {r['name']:<12} "
            f"{style(first, dim=dim)}"
        )
        for line in rest:
            click.echo(f"    {' ' * 12} {style(line, dim=dim)}")
    for line in _monitor_lines(monitors):
        click.echo(f"      {style(line, dim=True)}")
    click.echo()
    if failures:
        click.echo(f"  {style(f'{failures} check(s) failed.', fg='red', bold=True)}")
        sys.exit(1)
    click.echo(f"  {style('No failures.', fg='green', bold=True)}")
