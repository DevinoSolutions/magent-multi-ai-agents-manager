"""The `main` click group: entry point, argument parsing, and the
no-subcommand interactive dispatch. Kept alone in this module (importing
nothing from sibling command modules at top level) so every command module
can `from magent.cli.app import main` without a cycle -- see E6.md S2.1.
"""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import click

from magent.cli.checklist import ABORT_MESSAGE, choose_projects
from magent.cli.config_io import _load_config_or_exit
from magent.cli.ui import _open_in_editor
from magent.init_config import write_config
from magent.paths import find_config

# The error handler the entry point gives stdout (see _escape_unencodable_output).
OUTPUT_ERRORS = "magent.escape"
# What surrogateescape decodes an undecodable byte into: a lone U+DC80..U+DCFF
# IS the byte 0x80..0xFF it was read from, and writing it back means that byte.
_ESCAPED_BYTES = range(0xDC80, 0xDD00)
# The handlers a stdout is born with. Any other one was chosen on purpose -- a
# PYTHONIOENCODING=cp1252:replace, a harness's own wrapper -- and is kept.
_DEFAULT_ERRORS = frozenset({"strict", "surrogateescape"})


def _escape_or_restore(exc: UnicodeError) -> tuple[str | bytes, int]:
    """The ``magent.escape`` error handler: an undecodable byte is written back
    as itself, anything else unencodable as its escape.

    Both answers come from the stdlib handlers, never a copy of either: a lone
    U+DC80..U+DCFF gets surrogateescape's (the original byte, exactly what a
    stream that already used surrogateescape wrote -- so a POSIX path holding
    a non-UTF-8 byte still prints as that path), every other character
    backslashreplace's (``\\u4e2d``), other lone surrogates included. One call
    can be handed a run holding both kinds, so it answers only the run's
    leading same-kind prefix and returns where that prefix ends; the encoder
    resumes there and calls again for the rest.
    """
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    text = exc.object
    as_byte = ord(text[exc.start]) in _ESCAPED_BYTES
    stop = exc.start + 1
    while stop < exc.end and (ord(text[stop]) in _ESCAPED_BYTES) == as_byte:
        stop += 1
    prefix = UnicodeEncodeError(exc.encoding, text, exc.start, stop, exc.reason)
    stdlib = "surrogateescape" if as_byte else "backslashreplace"
    return codecs.lookup_error(stdlib)(prefix)


def _escape_unencodable_output() -> None:
    """Print a character stdout cannot encode as an escape, never a crash.

    On Windows a redirected stdout -- a pipe, a file, the Session-0 hand-off's
    out.txt, the ssh channel `magent attach` reads -- is the ANSI code page,
    whose surrogateescape (or, under PYTHONIOENCODING, strict) handler raises
    on anything that is not an escaped byte. So one project name or path it
    could not hold raised UnicodeEncodeError out of click.echo and exited 1;
    `up` got that far only after its sessions existed. Only the error handler
    changes: everything the stream could already encode is written
    byte-for-byte as before, an escaped byte is written back as that byte, and
    the rest becomes ``\\u4e2d`` (see ``_escape_or_restore``). This process
    only -- nothing is inherited and no environment is touched.

    Only a stream still on a default handler is changed, and its encoding
    never is: a Python parent that reads magent with text=True in its locale
    encoding must keep getting that encoding. Never raises on a byte-oriented
    encoding (a code page, UTF-8); a UTF-16/32 stdout, which only an explicit
    PYTHONIOENCODING gives, still raises on a lone U+DC80..U+DCFF, as it
    always did -- surrogateescape refuses those codecs.

    The ``--json`` emitters keep json.dumps's ensure_ascii default, so their
    output is ASCII and never reaches this handler; ensure_ascii=False would
    print ``\\U0001f600``, which is not a JSON escape.

    ``sys.stdout`` is None under pythonw, and a substitute stream may have no
    ``reconfigure``; both are left alone.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None or getattr(stream, "errors", None) not in _DEFAULT_ERRORS:
        return
    codecs.register_error(OUTPUT_ERRORS, _escape_or_restore)
    reconfigure(errors=OUTPUT_ERRORS)


@click.group(invoke_without_command=True)
@click.option("--go", is_flag=True, help="Skip interactive menu, launch + tile")
@click.option("--retile-all", is_flag=True, help="Re-tile every matching window")
@click.option("--dry-run", is_flag=True, hidden=True)
@click.option(
    "-a",
    "--all",
    "launch_all",
    is_flag=True,
    help="Launch every enabled project -- skip the project checklist",
)
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
@click.option(
    "--allow-dirty",
    is_flag=True,
    help="Node projects: start despite a dirty or unpushed tree",
)
# package_name (not a resolved literal) so click reads the distribution
# metadata inside the --version callback only -- see magent/__init__.py.
@click.version_option(package_name="magent-multi-ai-agents-manager")
@click.pass_context
def main(
    ctx: click.Context,
    go: bool,
    retile_all: bool,
    dry_run: bool,
    launch_all: bool,
    group: str | None,
    do_init: bool,
    base_dir: str | None,
    config_path: str | None,
    force: bool,
    do_edit: bool,
    attach_host: str | None,
    attach_port: int,
    attach_no_mux: bool,
    allow_dirty: bool,
) -> None:
    """Open every project in its own terminal and auto-tile across all monitors."""
    _escape_unencodable_output()
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

    # Which projects to launch. Off a terminal, or with `--all`, there is no
    # prompt and no narrowing -- `--go` in a script stays byte-for-byte what it
    # was. A pure re-tile launches nothing, so it is never asked either.
    tile_only = retile_all and not go
    only: frozenset[str] | None = None
    if not launch_all and not tile_only:
        choice = choose_projects(cfg, group=group)
        if choice.aborted:
            click.echo(ABORT_MESSAGE)
            return
        only = choice.only

    from magent.launch import (  # heavy subsystem: in-body per policy
        RunOpts,
        run_magent,
    )

    if not (dry_run or tile_only):
        # A node a project needs that cannot run it yet: set up inline at a
        # terminal, skipped in one line anywhere else. A dry run or a pure
        # re-tile never reads a node.
        from magent import nodes  # heavy subsystem: in-body per policy
        from magent.cli.node_onboard import ready_gate

        cfg = ready_gate(
            cfg,
            [
                p
                for p in nodes.node_projects(cfg, group)
                if only is None or nodes.project_name(p) in only
            ],
        )

    # Menu option 2 ("Re-tile all open windows") and a bare `--retile-all` both
    # promise tiling, not launching -- re-opening a window the user just closed
    # is what the flag is least expected to do. So `tile_only` means exactly
    # the open windows: configured projects that are up right now PLUS every
    # other magent-owned window on screen (`magent attach` panes), and nothing
    # closed. `--go` always carries the combined meaning: launch whatever is
    # missing, then tile EVERYTHING -- tiling only the new windows assigned
    # them slots from index 0 with the rest of the fleet ignored, so a top-up
    # --go dropped its one new window on top of an existing one and the user
    # had to run a manual retile to get the grid back.
    rc = run_magent(
        cfg,
        RunOpts(
            retile_all=retile_all or go,
            tile_only=tile_only,
            dry_run=dry_run,
            group=group,
            config_path=str(config_file),
            only=only,
            allow_dirty=allow_dirty,
        ),
    )
    if rc:
        sys.exit(rc)
