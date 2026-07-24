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
from magent.style import style


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
@click.option("--port", "-p", default=8033, help="Port to listen on")
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
def serve_cmd(ctx: click.Context, port: int, host: str | None, ensure: bool) -> None:
    """Start upload server for mobile image transfer.

    Opens a web page on your phone (via Tailscale) where you pick a project,
    upload an image, and the file path is auto-pasted into that project's
    Claude session via psmux send-keys.
    """
    from magent.upload_server import (  # heavy subsystem: in-body per policy
        run_server,
    )

    config_path = ctx.obj.get("config_path")
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
    help="Upload server port (default: running server, else 8033).",
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
        port = _running_upload_port() or 8033
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
