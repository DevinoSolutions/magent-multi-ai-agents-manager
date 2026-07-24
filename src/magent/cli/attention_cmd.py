"""The attention daemon command: `magent attention` (foreground),
`--daemon` (detached, pid file + heartbeat, shows in `status`), `--stop`.
Named attention_cmd (not "attention") to avoid confusion with
magent.attention, the engine/renderer subsystem it drives.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.lockfile import LockHeld, exclusive_lock
from magent.paths import find_config
from magent.procs import pid_alive
from magent.style import style
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from magent import attention
    from magent.config import AttentionSettings, MagentConfig
    from magent.platform import Platform

_PID_PATH = Path.home() / ".magent" / "attention.pid"

HEARTBEAT_NAME = "attention"

_NOTHING_TO_DO = (
    "nothing to do: every attention renderer is disabled or unsupported here"
)


def daemon_pid() -> int | None:
    """PID of the running attention daemon, or None. Clears a stale pid file."""
    try:
        pid = int(_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
        return pid
    with contextlib.suppress(OSError):
        _PID_PATH.unlink()
    return None


def _write_pid() -> None:
    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(os.getpid()))


def _clear_pid() -> None:
    with contextlib.suppress(OSError):
        if _PID_PATH.read_text().strip() == str(os.getpid()):
            _PID_PATH.unlink()


def stop_daemon() -> bool:
    """Stop the attention daemon; True only if a kill was issued and the
    process is confirmed gone. On failure the pid file is kept so `status`
    keeps reporting the truth."""
    # A forced kill (taskkill /F, default SIGTERM) doesn't run the daemon's
    # own finally/except, so this stop path owns the heartbeat cleanup: a clean
    # stop removes it, which is what distinguishes 'off' from 'crashed' in
    # status (P6-01).
    from magent.log import clear_heartbeat  # heavy subsystem: in-body per policy

    pid = daemon_pid()
    if not pid:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False
        )
        killed = result.returncode == 0
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
            killed = True
        except OSError:
            killed = False
    if killed and not pid_alive(pid):
        with contextlib.suppress(OSError):
            _PID_PATH.unlink()
        clear_heartbeat(HEARTBEAT_NAME)
        return True
    return killed and not pid_alive(pid)


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


def engine_from_config(cfg: MagentConfig) -> attention.AttentionEngine:
    """Build an AttentionEngine whose staleness/debounce come from
    ``settings.attention`` — so `status`/`watch` age states with the SAME
    config-driven windows as the daemon, not the module defaults. The name_map
    is derived from the enabled projects. Daemon-only concerns (renderers, ntfy
    topic) stay at the daemon call site; this helper covers the config-derived
    kwargs common to all three surfaces."""
    from magent import agent_state, attention  # heavy subsystem: in-body per policy

    att = cfg.settings.attention
    staleness = {
        agent_state.WORKING: att.staleness_working_s,
        agent_state.NEEDS_INPUT: att.staleness_needs_input_s,
    }
    return attention.AttentionEngine(
        attention.name_map_from_projects(name_pairs_from_config(cfg)),
        staleness=staleness,
        debounce_s=att.debounce_s,
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

                spawn_detached(args)
                for _ in range(20):
                    time.sleep(0.1)
                    pid = daemon_pid()
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
