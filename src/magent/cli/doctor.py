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

from magent import log, tailnet
from magent.cli.app import main
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
                "Windows Terminal (wt) not on PATH — nothing can launch. "
                f"Install: {WT_INSTALL_HINT}",
            )
        if get_platform().supports_psmux() and not psmux:
            return (WARN, "psmux not found — `up`/`attach` sessions unavailable")
        return (OK, "wt found" + (", psmux found" if psmux else ""))
    candidates = ("gnome-terminal", "konsole", "xterm", "alacritty", "kitty", "iTerm")
    found = [c for c in candidates if shutil.which(c)]
    if not found:
        return (WARN, "no known terminal emulator on PATH")
    return (OK, f"terminal: {found[0]}")


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


def _check_hotkey() -> CheckResult:
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if get_platform().supports_hotkey():
        return (OK, "Alt+V clipboard-upload hotkey available")
    return (OK, "hotkey not supported on this OS (Windows-only feature)")


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
            "MAGENT_SENTRY_DSN is set but sentry-sdk is missing — error "
            "reporting is OFF. sentry-sdk ships with magent, so this "
            f"install looks broken. Repair: {SENTRY_INSTALL_HINT}",
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
        ("monitors", _check_monitors),
        ("hotkey", _check_hotkey),
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
        click.echo(
            f"  {style(mark, fg=color, bold=True)} {r['name']:<12} "
            f"{style(r['detail'], dim=(r['status'] == OK))}"
        )
    for line in _monitor_lines(monitors):
        click.echo(f"      {style(line, dim=True)}")
    click.echo()
    if failures:
        click.echo(f"  {style(f'{failures} check(s) failed.', fg='red', bold=True)}")
        sys.exit(1)
    click.echo(f"  {style('No failures.', fg='green', bold=True)}")
