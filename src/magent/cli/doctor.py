"""`magent doctor` — environment diagnosis as a checklist.

`status` covers *daemons*; doctor covers *environment*: is the config
loadable and current, does the env validate, are the agent CLIs and a
terminal on PATH, can anything tile (monitors), are the runtime dirs
writable, is Tailscale reachable, is the upload port sane, are the nodes
healthy. Every check is a small function returning (status, detail) so
each is unit-testable; the command is just the runner. Exit 0 = no
failures (warns allowed), 1 = any check failed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

import click

from magent import log, psmux, tailnet
from magent.cli.app import main
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from magent.config import MagentConfig, ProjectConfig

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


def _cloud_config_problems(cfg: MagentConfig, cloud: list[ProjectConfig]) -> list[str]:
    """What the create gate would refuse for ``cloud`` from config alone, in the
    gate's own words and order (tool, then task). No git, no push set, no
    records, no live-session probe: those are the gate's to ask at create time.
    Then the pairs for one folder where the first enabled project owns the
    session name, so the other is never started -- the words `status` uses."""
    from magent import launch, nodes  # heavy subsystem: in-body per policy

    problems: list[str] = []
    for proj in cloud:
        tool = proj.tool or cfg.settings.default_tool
        refusal = launch.cloud_tool_refusal(tool, cfg.settings.tools.get(tool))
        if refusal is None and not proj.cloud_task:
            refusal = launch.NO_CLOUD_TASK
        if refusal:
            problems.append(f"{nodes.project_name(proj)}: {refusal}")
    for kind, shadows in (
        ("cloud", launch.shadowed_cloud_projects(cfg)),
        ("local", launch.shadowed_local_projects(cfg)),
    ):
        problems.extend(
            f"{kind} project {proj.path} is never started: "
            f"{launch.twin_session_refusal(sid)}"
            for proj, sid in shadows
        )
    return problems


def _claude_cloud_problem() -> str | None:
    """Why this PC's ``claude`` cannot start a cloud session, or None: not on
    PATH, would not run, or its help does not list ``--cloud``."""
    from magent import node_auth  # heavy subsystem: in-body per policy

    exe = node_auth.find_claude()
    if exe is None:
        return "cloud projects need the claude CLI on PATH"
    try:
        result = subprocess.run(
            [exe, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # The class only: the OS's own words carry an absolute path.
        return f"could not run claude --help ({type(exc).__name__})"
    if "--cloud" not in (result.stdout or ""):
        return "this claude has no --cloud: update Claude Code (claude update)"
    return None


def _check_cloud(cfg: MagentConfig | None) -> CheckResult:
    """Spec section 18: asked only when an ENABLED cloud project exists, WARN at
    worst. Reports what the create gate cannot see from here (does this claude
    have ``--cloud``) and the static config problems it would refuse with, then
    states the account facts a user must know before the first create. A paused
    or reclaimed cloud VM is never a failure, and nothing here calls it dead."""
    from magent.config import is_cloud  # heavy subsystem: in-body per policy

    cloud = [p for p in (cfg.projects if cfg else ()) if p.enabled and is_cloud(p)]
    if cfg is None or not cloud:
        return (OK, "no cloud projects")
    problems = _cloud_config_problems(cfg, cloud)
    claude_problem = _claude_cloud_problem()
    if claude_problem:
        problems.append(claude_problem)
    if problems:
        return (WARN, "; ".join(problems))
    return (
        OK,
        (
            "claude has --cloud; node push hands the push set off by hand.\n"
            "--cloud needs a claude.ai login (a setup-token does not authorize it) and is "
            "unavailable on Bedrock, Vertex or third-party providers, or when the "
            "allow_remote_sessions policy is off.\n"
            "Run /login and /web-setup once from a desktop terminal: a Session-0 pane "
            "cannot show the browser. An idle cloud session pauses and is reclaimed "
            "later; that is not a failure."
        ),
    )


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


def _check_daemons_session0() -> CheckResult:
    """Is one of magent's OWN daemons stranded in Session 0?

    The daemon spawn seams now refuse there, but an older magent -- or a
    foreground `magent serve` typed over ssh -- may already have left a serve,
    an Alt+V listener or an attention daemon behind. A Session-0 serve is the
    sharp one: it holds the loopback port this desktop's Alt+V needs, so the
    desktop's own serve dies of "port in use" while nothing it can see serves.

    WARN, never FAIL, for the psmux-session0 reason: this desktop cannot stop
    them, so they must not fail a doctor run. Asked only while a desktop
    exists -- on a headless host Session 0 is where daemons belong.
    """
    from magent.cli.status import session0_daemons, session0_daemons_message

    stranded = session0_daemons()
    if not stranded:
        return (OK, "no magent daemon runs in logon Session 0")
    return (WARN, session0_daemons_message(stranded))


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


def _check_nodes(cfg: MagentConfig | None) -> CheckResult:
    """Every configured node folded into one row -- WARN at worst: a node that
    is down or not set up yet is not this machine's environment failing. The
    per-node rows are `magent node doctor`'s; this reads the same report, so
    with nodes configured it costs up to one remote_mux.DOCTOR_TIMEOUT_S.

    A node's trouble is its fail and warn rows, named by item; any other row
    (ok, skip -- a node with nothing synced yet) is healthy."""
    if cfg is None:
        return (OK, "skipped -- config missing or invalid (see the config check)")
    if not cfg.settings.nodes:
        return (OK, "no nodes configured")
    # Sibling command modules, imported in-body so doctor.py's import never
    # depends on the registration hub's import order.
    from magent.cli import node_cmd
    from magent.cli.fleet_cmd import _stdout_safe

    report = node_cmd.doctor_report(cfg, list(cfg.settings.nodes))
    troubled: list[str] = []
    for nick, lines in report.items():
        items = [line.item for line in lines if line.status in (FAIL, WARN)]
        if items:
            troubled.append(f"{nick}: {', '.join(items)}")
    if not troubled:
        return (OK, f"{len(report)} node(s) healthy")
    # The items are the node's words (doctor.sh prints them), which a redirected
    # Windows stdout (cp1252) may not encode: lose a glyph, never the command.
    # A nick cannot need it, nor hold a separator: load_config admits only
    # [a-z0-9-]{1,6}.
    return (
        WARN,
        _stdout_safe("; ".join(troubled)) + " -- details: magent node doctor",
    )


# The lifecycle events whose records the idle reaper reads: working, done,
# needs-input and idle. Without them R7 vetoes every session.
_REAP_HOOK_EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionStart")


def _check_idle_reap(cfg: MagentConfig | None) -> CheckResult:
    """Will the idle reaper park anything? WARN at worst: a reaper that is off
    is a choice, so it is OK and names the gate, through ``reap.off_reason`` --
    the same translation serve's sweeps read. The WARN that matters is ON with
    the state hook unwired, which silently parks nothing, ever."""
    from magent import reap  # heavy subsystem: in-body per policy
    from magent.cli.hooks_cmd import (
        _default_settings_file,
        _event_wired,
        _load_settings,
    )
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if cfg is None:
        return (WARN, "config invalid or missing; the reaper stays off until it loads")
    off = reap.off_reason(cfg, get_platform())
    if off is not None:
        return (OK, reap.off_phrase(off))
    minutes = int(reap.threshold_s(cfg) // 60)
    configured = cfg.settings.idle_reap.after_minutes
    on = f"on, parks after {minutes} min idle"
    if configured != minutes:
        on += f" (afterMinutes={configured} raised to the {minutes}-min floor)"
    path = _default_settings_file()
    # _load_settings returns (never raises) why a file it cannot use is
    # unusable -- unreadable, not JSON, nested too deeply, or not the shape
    # Claude Code writes -- and `magent hooks status` reads the same answer.
    settings = _load_settings(path)
    if isinstance(settings, str):
        return (
            WARN,
            (
                f"{on}, but {path} is unreadable ({settings}); cannot tell whether "
                "the state hook is wired"
            ),
        )
    hooks = settings.get("hooks")
    wiring = hooks if isinstance(hooks, dict) else {}
    unwired = [e for e in _REAP_HOOK_EVENTS if not _event_wired(wiring.get(e))]
    if unwired:
        return (
            WARN,
            (
                f"{on}, but the state hook is not wired for {', '.join(unwired)}; "
                "nothing will ever be parked; run magent hooks install"
            ),
        )
    return (OK, on)


def _check_attention() -> CheckResult:
    """Is the attention daemon running, and if not, is that a fault.

    Reads the same state machine `status` renders (``cli.status.
    _attention_state``) so the two surfaces can never disagree. WARN at worst:
    without the daemon the fleet is quieter (no badges, flashes or pushes), not
    broken, and a serve that is up revives one that died. A daemon a restart
    took down is not a fault at all -- reporting it as a crash is exactly the
    wording this check exists to get right.
    """
    from magent.cli.status import _attention_state, _attention_supervised

    state = _attention_state()
    # Promised only when serve is allowed to keep that promise.
    revive = "; a running upload server restarts it" if _attention_supervised() else ""
    if state == "on":
        return (OK, "attention daemon running (heartbeat fresh)")
    if state == "stale":
        return (
            WARN,
            (
                "attention daemon process is alive but its heartbeat expired -- "
                "stop it with `magent attention --stop` and start it again"
            ),
        )
    if state == "crashed":
        return (
            WARN,
            (
                "attention daemon died without stopping cleanly (see "
                f"~/.magent/logs/attention.log{revive})"
            ),
        )
    if state == "off-since-restart":
        return (
            OK,
            (
                "attention daemon not running since the last restart "
                f"(start with `magent attention -d`{revive})"
            ),
        )
    return (OK, "attention daemon not running (start with `magent attention -d`)")


def _check_claude_token(cfg: MagentConfig | None) -> CheckResult:
    """The Claude token nodes sign in with (node_auth.token_health) -- WARN at
    worst, like the nodes row, and read only when nodes are configured: an
    ageing token on a PC without nodes is nobody's problem. Reads the file;
    never mints (a mint needs a person at a terminal, and doctor is a report)."""
    if cfg is None:
        return (OK, "skipped -- config missing or invalid (see the config check)")
    if not cfg.settings.nodes:
        return (OK, "no nodes configured")
    from magent import node_auth

    health = node_auth.token_health()
    if health.warning is not None:
        return (WARN, health.warning)
    if health.stored is None:
        return (
            WARN,
            (
                "no Claude token for nodes to sign in with yet -- run: "
                f"{node_auth.REFRESH_COMMAND}"
            ),
        )
    day = time.strftime("%Y-%m-%d", time.gmtime(health.stored.expires_at))
    return (OK, f"valid until {day}")


def _run_checks(config_file: Path) -> list[dict[str, str]]:
    (config_res, cfg) = _check_config(config_file)
    checks: list[tuple[str, CheckResult]] = [("config", config_res)]
    rest: list[tuple[str, Callable[[], CheckResult]]] = [
        ("env", _check_env),
        ("agent tools", lambda: _check_agent_tools(cfg)),
        ("cloud", lambda: _check_cloud(cfg)),
        ("terminal", _check_terminal),
        ("psmux wedge", _check_psmux_wedge),
        ("psmux-session0", _check_psmux_session0),
        ("daemons-session0", _check_daemons_session0),
        ("monitors", _check_monitors),
        ("hotkey", lambda: _check_hotkey(cfg)),
        ("attention", _check_attention),
        ("wt-keys", _check_wt_keys),
        ("logs dir", _check_logs_dir),
        ("state dir", _check_state_dir),
        ("sentry", _check_sentry),
        ("tailscale", _check_tailscale),
        ("upload port", lambda: _check_upload_port(cfg)),
        ("nodes", lambda: _check_nodes(cfg)),
        ("idle-reap", lambda: _check_idle_reap(cfg)),
        ("claude-token", lambda: _check_claude_token(cfg)),
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
