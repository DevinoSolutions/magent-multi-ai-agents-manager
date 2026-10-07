"""The attention daemon command: `magent attention` (foreground),
`--daemon` (detached, pid file + heartbeat, shows in `status`), `--stop`.
Named attention_cmd (not "attention") to avoid confusion with
magent.attention, the engine/renderer subsystem it drives.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

from magent import pidfile
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.lockfile import LockHeld, exclusive_lock
from magent.paths import find_config
from magent.procs import await_registration, pid_alive, predates_boot
from magent.style import style
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from magent import attention
    from magent.config import AttentionSettings, MagentConfig
    from magent.platform import Platform

_PID_PATH = Path.home() / ".magent" / "attention.pid"

HEARTBEAT_NAME = "attention"

_NOTHING_TO_DO = (
    "nothing to do: every attention renderer is disabled or unsupported here"
)


def daemon_pid() -> int | None:
    """PID of the running attention daemon, or None. Clears a stale pid file.

    The three verdicts (pre-boot, alive-but-unopenable, gone) are
    ``pidfile.read``'s. Believing a recycled pid would make `status` report a
    daemon that is not there and make serve's supervisor leave the real one
    dead.
    """
    return pidfile.read(_PID_PATH)


def _write_pid() -> None:
    pidfile.write(_PID_PATH)


def _clear_pid() -> None:
    pidfile.clear(_PID_PATH)


def stop_daemon() -> bool:
    """Stop the attention daemon; True only if a kill was issued and the
    process is confirmed gone. On failure the pid file is kept so `status`
    keeps reporting the truth."""
    # A forced kill doesn't run the daemon's own finally/except, so this stop
    # path owns the heartbeat cleanup: a clean stop removes it, which is what
    # distinguishes 'off' from 'crashed' in status (P6-01).
    from magent.log import clear_heartbeat  # heavy subsystem: in-body per policy

    pid = daemon_pid()
    if not pid:
        return False
    # Withdraw the heartbeat BEFORE the kill: serve revives a daemon that is
    # gone while its heartbeat lingers, and a heartbeat that outlived the kill
    # by one serve tick would turn this deliberate stop into a "crash" serve
    # undoes. Cleared again after the kill, in case the daemon pulsed between.
    clear_heartbeat(HEARTBEAT_NAME)
    _, outcome = pidfile.terminate(_PID_PATH)
    if outcome == "mismatch":
        # The number names a stranger: the file is stale and nothing was ended.
        pidfile.clear_stale(_PID_PATH)
        return False
    if outcome == "terminated" and not pid_alive(pid):
        pidfile.clear_stale(_PID_PATH)
        clear_heartbeat(HEARTBEAT_NAME)
        return True
    return False


def name_pairs_from_config(cfg: MagentConfig) -> list[tuple[str, str]]:
    """(display name, resolved path) for every enabled project — the input
    to attention.name_map_from_projects. Shared with status/watch."""
    from magent.launch import _resolve_path  # heavy subsystem: in-body per policy

    pairs: list[tuple[str, str]] = []
    for proj in cfg.projects:
        if not proj.enabled:
            continue
        resolved = _resolve_path(proj.path, cfg.base_dir) or proj.path
        pairs.append((proj.title or get_leaf_name(proj.path), resolved))
    return pairs


def staleness_from_config(cfg: MagentConfig) -> dict[str, float]:
    """``settings.attention``'s staleness keys as the ``{state: seconds}`` window
    map every state-aging surface takes.

    The ONE translation, and deliberately not inlined into the engine builder
    below: the engine is not the only reader. ``session_picker._session_states``
    ages the per-session rows behind `magent sessions` AND `status`'s
    psmux-session table, and it used to import ``attention.STALENESS_S``
    directly — so a widened window was honored by the daemon, `watch` and
    `status --json`'s agents array while those two surfaces silently kept the
    module defaults. The cli module owns the config translation and hands its
    consumers plain values."""
    from magent import agent_state  # heavy subsystem: in-body per policy

    att = cfg.settings.attention
    return {
        agent_state.WORKING: att.staleness_working_s,
        agent_state.NEEDS_INPUT: att.staleness_needs_input_s,
    }


def engine_from_config(cfg: MagentConfig) -> attention.AttentionEngine:
    """Build an AttentionEngine whose staleness/debounce come from
    ``settings.attention`` — so `status`/`watch` age states with the SAME
    config-driven windows as the daemon, not the module defaults. The name_map
    is derived from the enabled projects. Daemon-only concerns (renderers, ntfy
    topic) stay at the daemon call site; this helper covers the config-derived
    kwargs common to all three surfaces. When a project runs on a node, the
    engine also reads each placed node session's mirrored state store
    (node_sync.state_stores)."""
    from magent import attention, node_sync  # heavy subsystem: in-body per policy

    return attention.AttentionEngine(
        attention.name_map_from_projects(name_pairs_from_config(cfg)),
        staleness=staleness_from_config(cfg),
        debounce_s=cfg.settings.attention.debounce_s,
        # Node sessions' states, pulled home by `magent node sync`, keyed by
        # the node map (project -> nick, sid) and named by their project.
        extra_stores=node_sync.state_stores if node_sync.wanted(cfg) else None,
    )


def _plan_renderers(
    att_cfg: AttentionSettings,
    plat: Platform,
    engine: attention.AttentionEngine,
    ntfy_topic: str | None,
) -> tuple[list[attention.Renderer], list[str]]:
    """Build the enabled renderer set and collect any non-fatal prerequisite
    warnings (badges/flash unsupported here, ntfy on with no topic). An empty
    renderer list is the caller's fatal 'nothing to do' signal.

    Pure -- no console, no logging, no detach -- so the parent (`-d`) can
    validate prerequisites and fail fast on the still-attached console BEFORE
    spawning the detached child, and the child can log the identical result
    after detachment (P2-02)."""
    from magent import attention  # heavy subsystem: in-body per policy

    renderers: list[attention.Renderer] = []
    warnings: list[str] = []
    if plat.supports_attention_signals():
        if att_cfg.badge:
            renderers.append(attention.BadgeRenderer(plat))
        if att_cfg.flash:
            renderers.append(attention.FlashRenderer(plat))
    elif att_cfg.badge or att_cfg.flash:
        warnings.append("window badges/flash aren't supported on this OS")
    # notifyOnDone widens ONLY the two push channels (toast/ntfy) to also fire
    # on a done transition; flash/badge above are unaffected. With both off,
    # this set is computed but never reaches a renderer — notifyOnDone no-ops.
    push_states = attention.push_states(att_cfg.notify_on_done)
    if att_cfg.toast:
        renderers.append(attention.ToastRenderer(engine, push_states))
    if att_cfg.ntfy:
        if ntfy_topic:
            renderers.append(
                attention.NtfyRenderer(engine, str(ntfy_topic), push_states)
            )
        else:
            warnings.append(
                "attention.ntfy is on but MAGENT_NTFY_TOPIC is not set "
                "(see .env.example)"
            )
    return renderers, warnings


def _psmux_boost_tick() -> Callable[[], None]:
    """The per-tick psmux priority sweep (see ``psmux.boost_priority``).

    Unconditional -- no config gate and no second env gate -- because the sweep
    has exactly one gate of its own (``MAGENT_PSMUX_BOOST``, read inside
    ``boost_priority`` so every owner asks the same question) and is a no-op
    off Windows and on an already-boosted fleet. A daemon that ticked for hours
    while the fleet it watches typed slowly is precisely the gap this closes,
    and unlike the upload server there is nothing here to spawn, so there is
    nothing a user could be surprised by beyond the boost itself.
    """
    from magent.log import get_logger  # heavy subsystem: in-body per policy

    # psmux is a leaf, not a heavy subsystem -- in-body only to keep this
    # module's top-level import list matching its siblings' shape.
    from magent.psmux import boost_priority

    log = get_logger("attention")

    def _tick() -> None:
        try:
            boost_priority()
        except Exception:
            # Same doctrine as the upload watchdog below: supervision must
            # never take down the loop that is supposed to survive to look again.
            log.exception("psmux boost: priority sweep failed")

    return _tick


def _upload_watchdog(
    cfg: MagentConfig, config_path: str | None
) -> Callable[[list[attention.SessionView]], None] | None:
    """The per-tick hook that keeps ``magent serve`` alive, or None when this
    daemon must not supervise one.

    Two gates, and they answer different questions. ``settings.uploadServer`` is
    the config's own "does this machine run an upload server" switch: a user who
    turned it off is not second-guessed, and nothing is resurrected on a machine
    that never had one. ``MAGENT_UPLOAD_SUPERVISOR`` is the runtime opt-out for
    somebody who runs serve under their own supervisor -- and the reason every
    test fixture that starts a real ``attention -d`` can be sure it will not
    quietly spawn a real server on the runner.

    Riding the poll tick rather than a second timer is deliberate: the daemon
    already wakes on an interval, the probe is one refused loopback connect, and
    the RESPAWN rate is bounded by the supervisor's cooldown rather than by how
    often it looks (see ``launch.UploadServerSupervisor``).
    """
    from magent.launch import (  # heavy subsystem: in-body per policy
        UploadServerSupervisor,
        upload_supervision_enabled,
    )
    from magent.log import get_logger  # heavy subsystem: in-body per policy

    log = get_logger("attention")
    if not cfg.settings.upload_server:
        log.info("upload supervisor: off (settings.uploadServer is false)")
        return None
    if not upload_supervision_enabled():
        log.info("upload supervisor: disabled by MAGENT_UPLOAD_SUPERVISOR")
        return None
    supervisor = UploadServerSupervisor(cfg.settings.upload_port, config_path)
    log.info(
        "upload supervisor: watching port %d (respawn cooldown %.0fs)",
        cfg.settings.upload_port,
        supervisor.cooldown_s,
    )

    def _tick(_views: list[attention.SessionView]) -> None:
        try:
            supervisor.tick()
        except Exception:
            # Supervision must never be able to kill the daemon it rides on --
            # the whole point is that something survives to look again.
            log.exception("upload supervisor: check failed")

    return _tick


def _daemon_tick(
    cfg: MagentConfig, config_path: str | None
) -> Callable[[list[attention.SessionView]], None]:
    """Everything the daemon does per poll besides rendering: the psmux
    priority sweep (always) and the upload-server watchdog (when this daemon
    owns one). One hook, because ``run_attention_loop`` takes one."""
    boost = _psmux_boost_tick()
    watchdog = _upload_watchdog(cfg, config_path)

    def _tick(views: list[attention.SessionView]) -> None:
        boost()
        if watchdog is not None:
            watchdog(views)

    return _tick


def _setup_from_config(
    config_file: Path,
) -> tuple[
    attention.AttentionEngine,
    list[attention.Renderer],
    list[str],
    MagentConfig,
]:
    """Load config, build the engine, and plan renderers -- the shared setup
    for both the `-d` parent (validate-then-spawn) and the foreground/child
    (validate-then-run), so both judge prerequisites identically."""
    from magent.env import get_env  # heavy subsystem: in-body per policy
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    cfg = _load_config_or_exit(config_file)
    att = cfg.settings.attention
    plat = get_platform()
    engine = engine_from_config(cfg)
    topic = get_env().ntfy_topic
    renderers, warnings = _plan_renderers(
        att, plat, engine, str(topic) if topic else None
    )
    return engine, renderers, warnings, cfg


# --- serve's half of the mutual supervision ---------------------------------
# `attention -d` keeps `magent serve` alive (_upload_watchdog above); this is
# the other direction. Measured after a Windows restart: the bring-up brought
# serve and the Alt+V listener back, and nothing ever brought the attention
# daemon back -- `status` then called it CRASHED, which it had not. Serve is
# the process that is effectively always up, so it is the one that looks.
#
# The code lives here and not in upload_server.py, which only runs the hook:
# a src module must not import the cli package (LS-A-001), and this half needs
# the daemon's own pid/heartbeat/renderer judgement, all of which live here.
# DESIGN.md section 2 "The attention daemon is supervised by serve".

# Minimum seconds between two revive attempts. It must outlast the launcher's
# registration window (procs.REGISTRATION_TIMEOUT_S, pinned by test): the `-d`
# launcher holds the attention lock only until its child registers or that
# window runs out, and a second launcher started beside a child that is merely
# slow would end as two daemons.
ATTENTION_RESPAWN_COOLDOWN_S = 60.0


def attention_supervision_enabled() -> bool:
    """Whether MAGENT_ATTENTION_SUPERVISOR permits serve to keep the attention
    daemon alive. A serve must never die of an environment variable it does not
    use, so an env that no longer validates degrades to the default (supervise)
    with a log line -- the same posture as ``upload_supervision_enabled``."""
    from pydantic import ValidationError

    from magent.env import get_env  # heavy subsystem: in-body per policy
    from magent.log import get_logger  # heavy subsystem: in-body per policy

    try:
        return get_env().attention_supervisor
    except ValidationError:
        get_logger("attention").warning(
            "attention supervisor: environment did not validate; supervising anyway"
        )
        return True


def attention_daemon_argv(config_path: str | None) -> list[str]:
    """The argv serve runs to revive the daemon: the SAME `attention -d` a
    human types. Its lock and live-pid check are what guarantee there is never
    a second daemon, and its renderer validation is the daemon's own."""
    args = [sys.executable, "-m", "magent"]
    if config_path:
        args += ["--config", config_path]
    return [*args, "attention", "-d"]


class AttentionDaemonSupervisor:
    """Revives an attention daemon that was running and is gone, at most once
    per cooldown.

    "Was running" is the heartbeat file. Every clean stop removes it (`attention
    --stop`, `down --all`, Ctrl+C) and a crash, a kill or a restart leaves it --
    the same marker `status` already reads to tell "crashed" from "off". So a
    daemon the user never started, or stopped on purpose, is never started
    behind their back; one that died, or that a reboot took down, is.
    """

    def __init__(
        self,
        config_path: str | None = None,
        *,
        cooldown_s: float = ATTENTION_RESPAWN_COOLDOWN_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_path = config_path
        self._cooldown = cooldown_s
        self._now = now
        self._last_spawn: float | None = None
        self._said_no_work = False

    @property
    def cooldown_s(self) -> float:
        return self._cooldown

    def _has_work(self) -> bool:
        """Whether `attention -d` would start at all under the current config.

        Asked on every revive, not once: the config can change under a serve
        that runs for weeks. A config it cannot read, or one whose every
        renderer is off or unsupported, is a daemon that would exit 1 -- and
        spawning that once a cooldown forever is a respawn loop that can only
        fail. Said once in the log, not once a tick."""
        from pydantic import ValidationError

        from magent.config import load_config  # heavy subsystem: in-body per policy
        from magent.env import get_env  # heavy subsystem: in-body per policy
        from magent.log import get_logger  # heavy subsystem: in-body per policy
        from magent.platform import get_platform  # heavy subsystem: in-body per policy

        try:
            cfg = load_config(str(find_config(self._config_path)))
        except (ValueError, OSError) as e:
            reason = f"config unreadable ({e})"
        else:
            try:
                topic = get_env().ntfy_topic
            except ValidationError:
                topic = None
            renderers, _warnings = _plan_renderers(
                cfg.settings.attention,
                get_platform(),
                engine_from_config(cfg),
                str(topic) if topic else None,
            )
            if renderers:
                self._said_no_work = False
                return True
            reason = _NOTHING_TO_DO
        if not self._said_no_work:
            get_logger("attention").warning(
                "attention supervisor: not restarting the daemon: %s", reason
            )
            self._said_no_work = True
        return False

    def tick(self) -> bool:
        """One check. True when a revive was issued."""
        from magent.launch import (  # heavy subsystem: in-body per policy
            SESSION0_ATTENTION_REFUSAL,
            session0_block,
            spawn_detached,
        )
        from magent.log import (  # heavy subsystem: in-body per policy
            HEARTBEAT_MAX_AGE,
            get_logger,
            heartbeat_age,
        )
        from magent.platform import get_platform  # heavy subsystem: in-body per policy

        if daemon_pid() is not None:
            return False
        age = heartbeat_age(HEARTBEAT_NAME)
        if age is None:
            return False  # never started, or stopped cleanly
        if age <= HEARTBEAT_MAX_AGE:
            # Still fresh by status's own window: nothing proves the daemon
            # dead. Two ways to get here with no visible pid -- a pulse that
            # beat stop_daemon's kill and is not cleared yet (reviving undoes
            # `down --all`), and a daemon in Session 0 this desktop cannot open
            # (reviving starts a second one). A real crash stops pulsing and is
            # revived once the pulse goes stale.
            return False
        now = self._now()
        if self._last_spawn is not None and (now - self._last_spawn) < self._cooldown:
            return False
        # The same Session-0 seam as every other daemon spawn (launch.
        # session0_block): a daemon revived from a non-interactive logon session
        # would badge a desktop it cannot see and revive serve out of the
        # desktop's reach. attention_watchdog already declines to build this
        # supervisor there; this is the seam itself refusing, so no caller can
        # route around it. Stamped like a spawn so it is said once a cooldown.
        refusal = session0_block(SESSION0_ATTENTION_REFUSAL, get_platform())
        if refusal:
            self._last_spawn = now
            get_logger("attention").warning("attention supervisor: %s", refusal)
            return False
        if not self._has_work():
            return False
        self._last_spawn = now
        why = (
            "not running since the last restart"
            if predates_boot(time.time() - age)
            else "the daemon died without stopping cleanly"
        )
        # ASCII only: this line goes to a rotating logfile that gets read back
        # through whatever the host console's code page happens to be.
        get_logger("attention").warning(
            "attention supervisor: %s; starting magent attention -d", why
        )
        spawn_detached(attention_daemon_argv(self._config_path))
        return True


def attention_watchdog(config_path: str | None) -> Callable[[], None] | None:
    """The per-interval hook ``magent serve`` runs, or None when serve must not
    supervise the attention daemon.

    Two gates. ``MAGENT_ATTENTION_SUPERVISOR`` is the opt-out -- and the reason
    a test that starts a real serve can be sure no real daemon starts behind
    it. The logon-session disposition is the other: a serve in Windows logon
    Session 0 (a foreground `magent serve` over ssh) would start a daemon that
    badges a desktop nobody can see and holds the attention.pid the real
    desktop's daemon needs, so it only supervises where a launch would "run".
    """
    from magent.launch import (  # heavy subsystem: in-body per policy
        SESSION0_ATTENTION_REFUSAL,
        session0_block,
    )
    from magent.log import get_logger  # heavy subsystem: in-body per policy
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    log = get_logger("attention")
    if not attention_supervision_enabled():
        log.info("attention supervisor: disabled by MAGENT_ATTENTION_SUPERVISOR")
        return None
    refusal = session0_block(SESSION0_ATTENTION_REFUSAL, get_platform())
    if refusal:
        log.info("attention supervisor: off: %s", refusal)
        return None
    supervisor = AttentionDaemonSupervisor(config_path)
    log.info(
        "attention supervisor: watching the daemon (revive cooldown %.0fs)",
        supervisor.cooldown_s,
    )

    def _tick() -> None:
        try:
            supervisor.tick()
        except Exception:
            # Supervision must never be able to take down the server it rides
            # on -- that is the thing actually serving uploads.
            log.exception("attention supervisor: check failed")

    return _tick


def _handoff_daemon(config_path: str | None, interval: float | None) -> bool:
    """Hand `attention -d` to the desktop, or refuse, per policy.

    The same shape as `serve --ensure`'s gate (``cli/mobile._handoff_ensure``),
    because it is the same kind of command: it plants a detached survivor. Run
    over ssh on Windows that survivor lands in logon Session 0, badging windows
    on a desktop it cannot see and reviving the upload server out of this
    desktop's reach. True means this invocation is DONE (the desktop copy ran
    and this process exits with its code, or the policy refused).
    """
    from magent.launch import (  # heavy subsystem: in-body per policy
        SESSION0_ATTENTION_REFUSAL,
        SESSION0_ATTENTION_TIMEOUT_S,
        relay_handoff,
        session0_disposition,
        session0_refusal,
    )
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    plat = get_platform()
    disposition = session0_disposition(plat)
    if disposition == "run":
        return False
    if disposition == "refuse":
        reason = session0_refusal(plat, SESSION0_ATTENTION_REFUSAL)
        click.echo(f"  {style('x', fg='red')} {reason}", err=True)
        sys.exit(1)
    argv = [sys.executable, "-m", "magent"]
    if config_path:
        argv += ["--config", str(config_path)]
    argv += ["attention", "-d"]
    if interval is not None:
        argv += ["--interval", str(interval)]
    rc = relay_handoff(plat, argv, timeout_s=SESSION0_ATTENTION_TIMEOUT_S)
    if rc != 0:
        sys.exit(rc)
    return True


@main.command("attention")
@click.option("--daemon", "-d", "as_daemon", is_flag=True, help="Run detached")
@click.option("--stop", "do_stop", is_flag=True, help="Stop the running daemon")
@click.option(
    "--interval",
    default=None,
    type=float,
    help="Seconds between polls (default: attention.pollIntervalS from config)",
)
@click.option("--ticks", default=None, type=int, hidden=True)  # test seam
@click.pass_context
def attention_cmd(
    ctx: click.Context,
    as_daemon: bool,
    do_stop: bool,
    interval: float | None,
    ticks: int | None,
) -> None:
    """Ambient attention signals for your agent fleet.

    Badges every magent: window title with its session state, flashes the taskbar
    when an agent needs input or errors, and (when enabled in config) sends a
    Windows toast and/or an ntfy push. States come from the agent-state store
    that Claude Code hooks / Codex notify already write.
    """
    if do_stop:
        if stop_daemon():
            click.echo(f"  {style('+', fg='green')} Stopped the attention daemon.")
        else:
            click.echo(f"  {style('-', dim=True)} Attention daemon was not running.")
        return

    config_path = ctx.obj.get("config_path")
    config_file = find_config(config_path)

    if as_daemon:
        if _handoff_daemon(config_path, interval):
            return
        try:
            with exclusive_lock("attention"):
                existing = daemon_pid()
                if existing:
                    click.echo(
                        f"  {style('+', fg='green')} Attention daemon already running "
                        f"{style(f'(pid {existing})', dim=True)}"
                    )
                    return
                # P2-02: validate renderer prerequisites on the STILL-ATTACHED
                # console before detaching.
                _engine, renderers, warnings, _cfg = _setup_from_config(config_file)
                for warning in warnings:
                    click.echo(f"  {style('!', fg='yellow')} {warning}")
                if not renderers:
                    click.echo(f"  {style('x', fg='red')} {_NOTHING_TO_DO}")
                    sys.exit(1)

                args = [sys.executable, "-m", "magent"]
                if config_path:
                    args += ["--config", str(config_path)]
                args += ["attention"]
                # Forward --interval only when the user set it explicitly; an
                # unset (None) interval lets the detached child read
                # attention.pollIntervalS from config itself.
                if interval is not None:
                    args += ["--interval", str(interval)]
                from magent.launch import (  # heavy subsystem: in-body per policy
                    spawn_detached,
                )

                pid = await_registration(spawn_detached(args), daemon_pid)
                if pid:
                    click.echo(
                        f"  {style('+', fg='green')} Attention daemon running "
                        f"{style(f'(pid {pid})', dim=True)}"
                    )
                    return
                click.echo(f"  {style('x', fg='red')} attention daemon failed to start")
                sys.exit(1)
        except LockHeld:
            click.echo(
                f"  {style('+', fg='green')} Another attention daemon launch "
                f"is already in progress."
            )
            return

    # Foreground loop (also the body of the detached child).
    from magent import agent_state, attention  # heavy subsystem: in-body per policy
    from magent.log import (  # heavy subsystem: in-body per policy
        clear_heartbeat,
        get_logger,
        run_heartbeat,
        write_heartbeat,
    )

    engine, renderers, warnings, cfg = _setup_from_config(config_file)
    log = get_logger("attention")
    for warning in warnings:
        click.echo(f"  {style('!', fg='yellow')} {warning}")
        log.warning("%s", warning)
    if not renderers:
        click.echo(f"  {style('x', fg='red')} {_NOTHING_TO_DO}")
        # Detached child: the console is gone, so the startup-failure reason
        # only survives in the logfile (P2-02).
        log.error("%s", _NOTHING_TO_DO)
        sys.exit(1)

    state_ttl_s = cfg.settings.attention.state_ttl_days * 24 * 60 * 60
    log.info("attention loop starting: %d renderer(s)", len(renderers))
    agent_state.maybe_sweep_stale(ttl=state_ttl_s)
    click.echo(
        f"  {style('#', fg='cyan')} Watching {style(str(len(engine.poll())), bold=True)}"
        f" session(s) — Ctrl+C to stop."
    )
    _write_pid()
    # A dedicated heartbeat thread pulses at the fixed log.HEARTBEAT_INTERVAL,
    # decoupled from --interval: a user who widens --interval past the 30s
    # freshness window must not make `status` read a false 'stale' (P6-03).
    write_heartbeat(
        HEARTBEAT_NAME
    )  # immediate liveness before the thread's first pulse
    stop_hb = threading.Event()
    hb_thread = threading.Thread(
        target=run_heartbeat, args=(HEARTBEAT_NAME, stop_hb), daemon=True
    )
    hb_thread.start()
    stopped_cleanly = False
    try:
        poll_s = (
            interval if interval is not None else cfg.settings.attention.poll_interval_s
        )
        attention.run_attention_loop(
            engine,
            renderers,
            poll_interval=poll_s,
            max_ticks=ticks,
            on_tick=_daemon_tick(cfg, str(config_path) if config_path else None),
        )
    except KeyboardInterrupt:
        click.echo(f"\n  {style('Stopped.', dim=True)}")
        stopped_cleanly = True
    except Exception:
        # A crash leaves the heartbeat file behind on purpose: it is the marker
        # that lets status report 'crashed' instead of a healthy 'off' (P6-01).
        log.exception("attention daemon crashed")
        raise
    finally:
        # Stop and JOIN the heartbeat thread before touching the file, so no
        # late pulse can re-create it after a clean stop (which would masquerade
        # as a crash). Only Ctrl+C is a clean in-process stop; a crash keeps the
        # heartbeat as its marker, and an external kill is handled by stop_daemon.
        stop_hb.set()
        hb_thread.join(timeout=5)
        if stopped_cleanly:
            clear_heartbeat(HEARTBEAT_NAME)
        # Inverse-transience: strip any badges we set so a stopped daemon never
        # leaves a frozen [!]/[x]/[+] glyph misrepresenting state (P6-06). The
        # BadgeRenderer is the only renderer that tracks what it set.
        for renderer in renderers:
            if isinstance(renderer, attention.BadgeRenderer):
                renderer.clear_badges()
        _clear_pid()
