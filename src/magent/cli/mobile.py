"""Upload-server / QR / Termius daemon commands: `serve`, `mobile`, `termius`.
Carries E8's serve `--host` bind-address option.
"""

from __future__ import annotations

import getpass
import re
from pathlib import Path

import click

from magent import tailnet
from magent.cli.app import main
from magent.cli.background import (
    _maybe_start_upload_server,
    _running_upload_port,
    _tailnet_host,
)
from magent.cli.ui import _banner, _divider, _force_utf8_console, _print_qr
from magent.config import load_config
from magent.paths import find_config
from magent.style import style

# The port the upload server bound before it read the config at all -- and
# therefore the only honest answer when there is no config to read. Same
# literal the parser falls back to for a missing `uploadPort`
# (config._parse_settings / Settings.upload_port).
_FALLBACK_UPLOAD_PORT = 8033


def _configured_upload_port(config_path: str | None) -> int:
    """``settings.uploadPort`` of the config this invocation would use, or
    ``_FALLBACK_UPLOAD_PORT`` when there is no readable config.

    Deliberately NOT ``config_io._load_config_or_exit``: ``serve`` has always
    started with no config at all (``run_server`` only forwards the path to the
    per-project lookup), so a missing or invalid config has to stay a fallback
    here rather than become a hard exit -- teaching serve to read the config
    must not make it start failing where it used to work. Same tolerant read as
    ``doctor._check_config``.
    """
    config_file = find_config(config_path)
    if not config_file.exists():
        return _FALLBACK_UPLOAD_PORT
    try:
        # ConfigError <: ValueError (bad JSON, wrong-typed field);
        # FileNotFoundError <: OSError covers a config that vanished/unreadable
        # between the exists() check and the read.
        return load_config(str(config_file)).settings.upload_port
    except (ValueError, OSError):
        return _FALLBACK_UPLOAD_PORT


@main.command("termius")
@click.option("--host", default=None, help="SSH hostname or IP (default: Tailscale IP)")
@click.option("--user", default=None, help="SSH username (default: current user)")
@click.option("--install", is_flag=True, help="Write entry to ~/.ssh/config")
@click.pass_context
def termius_cmd(
    ctx: click.Context, host: str | None, user: str | None, install: bool
) -> None:
    """Generate SSH config for Termius — one host that opens all projects.

    Connects to the 'magent' psmux session with all project windows inside.
    Switch windows with Ctrl+B then number/name.
    """

    if not host:
        host = tailnet.ip4()
        if not host:
            host = click.prompt(
                f"  {style('SSH host/IP', fg='cyan')}", default="localhost"
            )

    if not user:
        user = getpass.getuser()

    marker_start = "# --- magent-start ---"
    marker_end = "# --- magent-end ---"

    block = f"""{marker_start}
Host magent
    HostName {host}
    User {user}
    RemoteCommand magent sessions
    RequestTTY force
{marker_end}"""

    if install:
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(exist_ok=True)
        ssh_config = ssh_dir / "config"

        existing = ssh_config.read_text(encoding="utf-8") if ssh_config.exists() else ""

        if marker_start in existing:
            pattern = re.escape(marker_start) + r".*?" + re.escape(marker_end)
            updated = re.sub(pattern, block, existing, flags=re.DOTALL)
        else:
            updated = (
                existing.rstrip() + "\n\n" + block + "\n" if existing else block + "\n"
            )

        ssh_config.write_text(updated, encoding="utf-8")
        click.echo(
            f"  {style('+', fg='green', bold=True)} Wrote {style('magent', fg='cyan', bold=True)} host to {style(str(ssh_config), dim=True)}"
        )
        click.echo()
        click.echo(
            f"  {style('SSH in:', bold=True)} {style('ssh magent', fg='cyan')} {style('— shows session picker.', dim=True)}"
        )
        click.echo(f"  {style('Pick a project, F1 to go back to the list.', dim=True)}")
    else:
        click.echo(block)
        click.echo()
        click.echo(f"  {style('Add --install to write to ~/.ssh/config', dim=True)}")


@main.command("serve")
@click.option(
    "--port",
    "-p",
    default=None,
    type=int,
    help="Port to listen on (default: the config's upload_port)",
)
@click.option(
    "--host",
    default=None,
    help="Bind a specific address instead of the default "
    "(loopback + Tailscale IP, never the LAN wildcard). "
    "Pass 0.0.0.0 to restore an explicit LAN-wide bind.",
)
@click.option(
    "--ensure",
    is_flag=True,
    help="Start the server detached if it isn't already running, then exit (used by attach).",
)
@click.pass_context
def serve_cmd(
    ctx: click.Context, port: int | None, host: str | None, ensure: bool
) -> None:
    """Start upload server for mobile image transfer.

    Opens a web page on your phone (via Tailscale) where you pick a project,
    upload an image, and the file path is auto-pasted into that project's
    Claude session via psmux send-keys.
    """
    from magent.upload_server import (  # heavy subsystem: in-body per policy
        run_server,
    )

    config_path = ctx.obj.get("config_path")
    # No `-p` means "the port this machine's config says", not a hard-coded
    # 8033: `status`/`up`/`down` all watch `settings.uploadPort`, so a serve
    # that ignored it bound a port nothing else was looking at and every other
    # surface reported the upload server dead. An explicit `-p` still wins.
    if port is None:
        port = _configured_upload_port(config_path)
    if ensure:
        # Non-blocking: ensure a survivor server exists on this port, then return.
        # attach calls this over SSH so the host always has a server for Alt+V,
        # regardless of the uploadServer config flag or whether anything was
        # just brought up.
        _maybe_start_upload_server(port, config_path)
        click.echo(f"upload server ensured on port {port}")
        return

    ip = tailnet.ip4()

    _banner()
    click.echo(
        f"  {style('Upload server', bold=True)}  {style('for mobile image transfer', dim=True)}"
    )
    _divider()
    click.echo()
    if ip:
        click.echo(
            f"  {style('Open on phone:', bold=True)}  {style(f'http://{ip}:{port}', fg='cyan', bold=True)}"
        )
    click.echo(
        f"  {style('Local:', dim=True)}         {style(f'http://localhost:{port}', fg='cyan')}"
    )
    click.echo()
    click.echo(
        f"  {style('Pick a project, upload a file, path gets pasted into Claude.', dim=True)}"
    )
    click.echo(f"  {style('Ctrl+C to stop.', dim=True)}")
    click.echo()

    try:
        run_server(port=port, config_path=config_path, host=host)
    except KeyboardInterrupt:
        click.echo(f"\n  {style('Server stopped.', dim=True)}")


@main.command("mobile")
@click.option(
    "--port",
    "-p",
    default=None,
    type=int,
    help="Upload server port (default: running server, else the config's upload_port).",
)
@click.option(
    "--host",
    default=None,
    help="Host/IP for the phone URL (default: Tailscale name or IP).",
)
@click.pass_context
def mobile_cmd(ctx: click.Context, port: int | None, host: str | None) -> None:
    """Show the phone URL + QR for the image-upload app.

    Scan it once on your phone, then 'Add to Home Screen' to install the
    uploader as a standalone app -- after that it's one tap to send an image
    into any magent: session. Run this on the host that serves the uploader.
    """
    _force_utf8_console()
    if port is None:
        # A live server's real port first (that URL is the one that works right
        # now), then what the config asks for, then the historical default --
        # never a hard-coded 8033 while the config says otherwise.
        port = _running_upload_port() or _configured_upload_port(
            ctx.obj.get("config_path")
        )
    if not host:
        host = _tailnet_host()
    url = f"http://{host}:{port}/"

    _banner()
    click.echo(
        f"  {style('Mobile uploader', bold=True)}  {style('- install as a home-screen app', dim=True)}"
    )
    _divider()
    click.echo()
    click.echo(
        f"  {style('Open on phone:', bold=True)}  {style(url, fg='cyan', bold=True)}"
    )
    click.echo()
    _print_qr(url)
    click.echo()
    click.echo(
        f"  {style('Install:', bold=True)}  {style('iOS', fg='cyan')} Share {style('>', dim=True)} Add to Home Screen"
        f"     {style('Android', fg='cyan')} menu {style('>', dim=True)} Add to Home screen"
    )
    click.echo(
        f"  {style('Then it opens straight to the uploader - pick a project, send an image.', dim=True)}"
    )
    click.echo()
