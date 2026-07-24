"""The `main` click group: entry point, argument parsing, and the
no-subcommand interactive dispatch. Kept alone in this module (importing
nothing from sibling command modules at top level) so every command module
can `from magent.cli.app import main` without a cycle -- see E6.md S2.1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from magent import __version__
from magent.cli.config_io import _load_config_or_exit
from magent.cli.ui import _open_in_editor
from magent.init_config import write_config
from magent.paths import find_config


@click.group(invoke_without_command=True)
@click.option("--go", is_flag=True, help="Skip interactive menu, launch + tile")
@click.option("--retile-all", is_flag=True, help="Re-tile every matching window")
@click.option("--dry-run", is_flag=True, hidden=True)
@click.option("-g", "--group", default=None, help="Launch only projects in this group")
@click.option("--init", "do_init", is_flag=True, help="Re-scan and regenerate config")
@click.option(
    "--base-dir", default=None, type=click.Path(), help="Folder to scan with --init"
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="Path to config file",
)
@click.option("--force", is_flag=True, help="With --init, overwrite existing config")
@click.option(
    "--edit", "do_edit", is_flag=True, help="Open config in your default editor"
)
@click.option(
    "--attach-to",
    "attach_host",
    default=None,
    help="Attach to remote psmux sessions (host or user@host)",
)
@click.option(
    "--attach-port",
    default=8033,
    hidden=True,
    help="(deprecated) port is now read from the host config",
)
@click.option(
    "--no-mux",
    "attach_no_mux",
    is_flag=True,
    help="With --attach-to: one plain SSH window per project (no psmux/tmux)",
)
@click.version_option(__version__)
@click.pass_context
def main(
    ctx: click.Context,
    go: bool,
    retile_all: bool,
    dry_run: bool,
    group: str | None,
    do_init: bool,
    base_dir: str | None,
    config_path: str | None,
    force: bool,
    do_edit: bool,
    attach_host: str | None,
    attach_port: int,
    attach_no_mux: bool,
) -> None:
    """Open every project in its own terminal and auto-tile across all monitors."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path

    from pydantic import ValidationError  # heavy subsystem: in-body per policy

    from magent import env as env_module  # heavy subsystem: in-body per policy

    try:
        env = env_module.get_env()
    except ValidationError as exc:
        for name, msg in env_module.validation_error_items(exc):
            prefix = f"{name}: " if name else ""
            click.echo(f"{prefix}{msg}", err=True)
        where = (
            f"; env file: {env_module.ENV_FILE}" if env_module.ENV_FILE.exists() else ""
        )
        click.echo(
            f"Fix the environment variable(s) above (see .env.example{where}).",
            err=True,
        )
        sys.exit(1)
    if env.sentry_dsn:
        from magent.sentry import init_sentry  # heavy subsystem: in-body per policy

        init_sentry(str(env.sentry_dsn))

    if ctx.invoked_subcommand is not None:
        return

    # cycle-break: app.py cannot import sibling command modules at top level
    # (the registration hub imports every command module, which imports
    # app.main back) -- these handlers are only needed on the interactive/
    # no-subcommand path.
    from magent.cli import (
        _attach_flow,
        _menu_down,
        _menu_status,
        _menu_up,
        _run_discovery,
        _run_sessions_picker,
        _show_menu,
    )

    if attach_host:
        _attach_flow(attach_host, no_mux=attach_no_mux, group=group)
        return

    config_file = find_config(config_path)

    if do_edit:
        if not config_file.exists():
            click.echo(f"No config at {config_file}. Run magent first to generate one.")
            sys.exit(1)
        _open_in_editor(config_file)
        return

    if do_init:
        if base_dir:
            root = Path(base_dir).resolve()
            if not root.is_dir():
                click.echo(f"Folder not found: {base_dir}", err=True)
                sys.exit(1)
            success, skipped = write_config(str(root), str(config_file), force=force)
            if success:
                click.echo(f"Wrote config to {config_file}")
                if skipped:
                    noun = "directory" if skipped == 1 else "directories"
                    click.echo(f"Skipped {skipped} unreadable {noun}.", err=True)
            else:
                click.echo(
                    f"{config_file} exists -- use --force to overwrite.", err=True
                )
                sys.exit(1)
        else:
            if config_file.exists() and not force:
                click.echo(
                    f"{config_file} exists -- use --force to overwrite.", err=True
                )
                sys.exit(1)
            _run_discovery(config_file)
        return

    if not config_file.exists():
        if config_path:
            click.echo(f"No config found at: {config_file}", err=True)
            sys.exit(1)
        if sys.stdin.isatty() and not go:
            wrote = _run_discovery(config_file)
            if not wrote:
                sys.exit(1)
        elif not config_file.exists():
            click.echo("No config found. Run: magent --init", err=True)
            sys.exit(1)

    cfg = _load_config_or_exit(config_file)

    has_directive = go or retile_all or dry_run or group
    if not has_directive and sys.stdin.isatty():
        while True:
            groups = sorted({p.group for p in cfg.projects if p.group})
            menu = _show_menu(list(groups), config_file)
            action = menu["action"]
            if action == "quit":
                return
            if action == "attach":
                _attach_flow(None, no_mux=False)
                return
            if action == "sessions":
                _run_sessions_picker(config_file)
                continue
            if action == "status":
                _menu_status(config_file)
                continue
            if action == "up":
                _menu_up(config_file)
                continue
            if action == "down":
                _menu_down(config_file)
                continue
            if menu.get("reload"):
                cfg = _load_config_or_exit(config_file)
            retile_all = bool(menu["retile_all"])
            group_choice = menu.get("group")
            group = group_choice if isinstance(group_choice, str) else None
            break

    from magent.launch import (  # heavy subsystem: in-body per policy
        RunOpts,
        run_magent,
    )

    rc = run_magent(
        cfg,
        RunOpts(
            retile_all=retile_all,
            dry_run=dry_run,
            group=group,
            config_path=str(config_file),
        ),
    )
    if rc:
        sys.exit(rc)
