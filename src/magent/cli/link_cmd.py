"""`magent link` / `magent unlink`: pair this machine with the magent app
through the relay (spec 4.1), show the link, forget it.

The shell owns every print, prompt and exit code; pairing itself is
``linkpair``, the files ``linkfiles``, the transport ``link``. Nothing here
prints or logs the host token, the poll token or a private key, and the QR
link (it carries the pairing secret) is printed only when ``--no-qr`` asks
for it.

The order of a new pairing is the safety rule: nothing is stored before the
device is trusted (its QR proof verified, or the person confirming the
safety number); a refusal or Ctrl+C at that point tells the relay the
pairing failed (``accept_claim`` frees the new host); remote control is
asked only after that. A new host the relay created that this machine could
not save (a failed write, Ctrl+C during it) is freed on the relay as well.
"""

from __future__ import annotations

import contextlib
import json
import platform
import socket
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING, NoReturn

import click

from magent.cli.app import main
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

    from magent.link import RelayClient
    from magent.linkcrypto import KeyPairs
    from magent.linkfiles import LinkFile
    from magent.linkpair import ClaimOutcome

PAIR_WAIT_S = 600.0  # the code lives 10 minutes on the relay...
PAIR_WAIT_SLACK_S = (
    30.0  # ...and a clock a little behind the relay's still waits it out
)
DEVICES_MAX = 10  # devices one host trusts; --add-device keeps the newest
NAME_SHOWN_MAX = 64  # characters of a relay-sent name shown, escapes counted
RELEASE_TIMEOUT_S = 10.0  # one DELETE /v1/host, never retried
DEFAULT_UPLOAD_PORT = 8033
REMOTE_CONTROL_PROMPT = (
    "Allow devices paired with this host to send text, choose, interrupt, "
    "change model, start/stop sessions and edit projects?"
)
SAFETY_NUMBER_HELP = (
    "Answer y only if the app shows this same number. "
    "If you scanned the QR code, the app shows no number: answer N."
)


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    sys.exit(1)


def _shown(text: str, limit: int = NAME_SHOWN_MAX) -> str:
    """``text`` as this terminal may show it: printable ASCII as it is, any
    other character (and the backslash) as its escape, cut to ``limit``
    characters plus "...". A name the relay forwards is the device's to
    choose; it never writes to the screen it is shown on."""
    out: list[str] = []
    size = 0
    for ch in text:
        piece = (
            ch
            if " " <= ch <= "~" and ch != "\\"
            else ch.encode("unicode_escape").decode("ascii")
        )
        if size + len(piece) > limit:
            return "".join(out) + "..."
        out.append(piece)
        size += len(piece)
    return "".join(out)


def _settings(config_file: Path) -> tuple[int, bool]:
    """(uploadPort, settings.link.remoteControl) from ``config_file``; the
    defaults (8033, off) when it does not load."""
    from magent.config import load_config  # heavy subsystem: in-body per policy

    try:
        settings = load_config(str(config_file)).settings
    except (OSError, ValueError):  # ConfigError is a ValueError
        return DEFAULT_UPLOAD_PORT, False
    return settings.upload_port, settings.link.remote_control


def _serving(config_file: Path) -> bool:
    """Does the configured port's ``magent serve`` answer /health? Only one
    input: who runs the link is the link lock's answer (``_link_report``)."""
    from magent.cli.status import _upload_state

    return _upload_state(_settings(config_file)[0]) == "on"


def _other_config_note(config_file: Path) -> None:
    """Say so when the serve running the link reads remote control from
    another file than the one this command just wrote."""
    from magent import link  # heavy subsystem: in-body per policy
    from magent.cli.status import _config_mismatch

    note = _config_mismatch(link.link_serve(), config_file)
    if note is not None:
        click.echo(f"Note: {note}")


def _set_remote_control(config_file: Path, value: bool) -> None:
    from magent import link  # heavy subsystem: in-body per policy

    if not config_file.exists():
        click.echo(
            f"No config found at {config_file}; remote control stays off. "
            "Run magent first, then `magent link --remote-control`."
        )
        return
    try:
        link.set_remote_control(config_file, value)
    except (OSError, ValueError) as exc:  # LockHeld, ConfigWriteError included
        _fail(f"Could not save settings.link.remoteControl: {exc}")
    click.echo(f"Remote control: {'on' if value else 'off'}.")
    _other_config_note(config_file)


def _confirm_number(number: str) -> bool:
    click.echo(SAFETY_NUMBER_HELP)
    return click.confirm(f"Does the app show {number}?", default=False)


def _show_status(config_file: Path, as_json: bool) -> None:
    from magent import link, linkfiles  # heavy subsystem: in-body per policy
    from magent.cli.status import _link_report

    port, remote_control = _settings(config_file)
    report = _link_report(
        lambda: _serving(config_file), port=port, config_file=config_file
    )
    status = report.status
    try:
        linked = linkfiles.load_link()
    except linkfiles.LinkFileError:
        linked = None
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "link": status.as_json(),
                    "relay_url": link.shown_url(linked.relay_url) if linked else None,
                    "host_id": linked.host_id if linked else None,
                    "devices": [
                        {
                            "device_id": d.device_id,
                            "name": d.name,
                            "paired_at": d.paired_at,
                        }
                        for d in (linked.devices if linked else ())
                    ],
                    "remote_control": remote_control,
                }
            )
        )
        return
    click.echo(f"  {style('Link', bold=True)}             {status.detail}")
    for line in (*report.notes, status.hint):
        if line:
            click.echo(f"  {style(line, dim=True)}")
    for warning in report.warnings:
        click.echo(f"  {style('!', fg='yellow')} {style(warning, fg='yellow')}")
    if linked is None:
        return
    click.echo(
        f"  {style('Relay', bold=True)}            {link.shown_url(linked.relay_url)}"
    )
    click.echo(f"  {style('Host', bold=True)}             {_shown(linked.host_id)}")
    for device in linked.devices:
        click.echo(
            f"  {style('Device', bold=True)}           {_shown(device.name)}  "
            f"{style('paired ' + _shown(device.paired_at), dim=True)}"
        )
    click.echo(
        f"  {style('Remote control', bold=True)}   {'on' if remote_control else 'off'}"
    )


def _keys_for(existing: LinkFile | None) -> KeyPairs:
    """Fresh host keys, or the saved host's for --add-device."""
    from magent import linkcrypto  # heavy subsystem: in-body per policy

    try:
        if existing is None:
            return linkcrypto.generate_key_pairs()
        return linkcrypto.public_from_private(
            linkcrypto.b64u_decode(existing.host_kex_priv),
            linkcrypto.b64u_decode(existing.host_sign_priv),
        )
    except linkcrypto.LinkCryptoUnavailable:
        _fail(f"magent link needs the [link] extra: {linkcrypto.INSTALL_HINT}")
    except ValueError:
        _fail(
            "magent link: link.json holds a key that cannot be used; "
            "run `magent unlink`, then `magent link`."
        )


def _claim(
    client: RelayClient, keys: KeyPairs, existing: LinkFile | None, no_qr: bool
) -> ClaimOutcome:
    """Start the pairing, show the code, wait for the claim, judge it."""
    from magent import __version__, link, linkpair  # heavy subsystem: in-body
    from magent.cli.ui import _print_qr

    try:
        start = linkpair.start_pairing(
            client,
            keys,
            name=socket.gethostname(),
            platform=platform.system().lower() or "unknown",
            version=__version__,
            host_token=existing.host_token if existing is not None else None,
        )
        url = linkpair.qr_url(start, keys)
        code = linkpair.display_code(start.code)
        click.echo("Pair this machine with the magent app.")
        if no_qr:
            click.echo(f"Open this link on the device: {url}")
        else:
            _print_qr(url)
            click.echo("Scan the QR code with the device's camera,")
        click.echo(f"or open {start.app_url}/pair and enter the code {code}.")
        click.echo(
            "Waiting for the app (the code expires in 10 minutes; Ctrl+C cancels)..."
        )
        # The relay's expiry, but never longer than its 10 minutes (and a
        # little slack) by this clock: a relay cannot hold the wait open.
        deadline = min(
            linkpair.deadline_of(start), time.time() + PAIR_WAIT_S + PAIR_WAIT_SLACK_S
        )
        poll = linkpair.wait_for_claim(client, start, deadline=deadline)
        return linkpair.accept_claim(
            poll, start, keys, confirm=_confirm_number, client=client
        )
    except (linkpair.PairingRateLimited, linkpair.PairingRejected) as exc:
        _fail(f"magent link: {exc}")
    except linkpair.PairingExpired:
        _fail("The code expired before a device claimed it. Run `magent link` again.")
    except linkpair.AppUrlRefused:
        _fail(
            f"magent link: this relay's app is not {link.shown_url(link.app_url())}; "
            "set MAGENT_LINK_APP_URL if you run your own app"
        )
    except link.TooOld:
        _fail(link.UPGRADE_HINT)
    except link.Revoked:
        _fail(
            "magent link: the relay no longer accepts this machine's host token; "
            "run `magent unlink`, then `magent link`."
        )
    except link.RelayError as exc:
        _fail(
            f"magent link: the relay at {link.shown_url(client.base_url)} answered: "
            f"{_shown(str(exc), 300)}"
        )
    except ValueError as exc:
        _fail(f"magent link: {_shown(str(exc), 300)}")


def _release(client: RelayClient, token: str) -> None:
    """DELETE /v1/host for a new host this machine could not keep. Best
    effort, one attempt: the host token is never shown either way."""
    from magent import link  # heavy subsystem: in-body per policy

    try:
        client.call(
            "DELETE", "/v1/host", token=token, retries=0, timeout=RELEASE_TIMEOUT_S
        )
    except (link.RelayError, ValueError):
        click.echo(
            "The relay could not be told to forget the new host; "
            "remove it on the app's Hosts page.",
            err=True,
        )


def _save_new_host(client: RelayClient, keys: KeyPairs, outcome: ClaimOutcome) -> None:
    """link.json for the new host, behind a fresh link-state.json (cursor 0,
    no seen ids, this host's id): written first, while no runner reads it,
    so the runner the new link.json starts can only load the fresh one. A
    failure, or Ctrl+C, frees the host on the relay and puts the old state
    back."""
    from magent import linkcrypto, linkfiles  # heavy subsystem: in-body per policy

    token = outcome.host_token
    if token is None:  # accept_claim refuses a new host without one
        _fail("The relay did not issue a host token; nothing was saved.")
    had_state = linkfiles.LINK_STATE_PATH.exists()
    previous = linkfiles.load_state()
    try:
        linkfiles.save_state(
            linkfiles.LinkState(state="connecting", host_id=outcome.host_id)
        )
        linkfiles.save_link(
            linkfiles.LinkFile(
                relay_url=client.base_url,
                host_id=outcome.host_id,
                host_token=token,
                host_kex_priv=linkcrypto.b64u_encode(keys.kex_priv),
                host_sign_priv=linkcrypto.b64u_encode(keys.sign_priv),
                devices=outcome.devices,
            )
        )
    except BaseException as exc:
        _release(client, token)
        # Best effort: the runner reads a state of another pairing as fresh.
        with contextlib.suppress(OSError):
            if had_state:
                linkfiles.save_state(previous)
            else:
                linkfiles.LINK_STATE_PATH.unlink(missing_ok=True)
        if not isinstance(exc, Exception):
            raise  # Ctrl+C stays Ctrl+C
        _fail(
            f"magent link: could not save {linkfiles.LINK_PATH} "
            f"({type(exc).__name__}); the relay was told to forget this pairing. "
            "Run `magent link` again."
        )


def _save_added_device(existing: LinkFile, outcome: ClaimOutcome) -> None:
    """Merge the new device into link.json as it is now (the runner keeps
    the device list current): same host and token or nothing is saved, one
    entry per device id, at most DEVICES_MAX (the oldest go)."""
    from magent import linkfiles  # heavy subsystem: in-body per policy

    if outcome.host_id != existing.host_id:
        _fail(
            f"magent link: the relay paired the device with host "
            f"{_shown(outcome.host_id)}, not this machine's "
            f"{_shown(existing.host_id)}; nothing was saved. Remove the device "
            "in the app."
        )
    try:
        current = linkfiles.load_link() or existing
    except linkfiles.LinkFileError:
        current = existing
    if (current.host_id, current.host_token) != (existing.host_id, existing.host_token):
        _fail(
            "magent link: link.json changed while the device was pairing; "
            "nothing was saved. Run `magent link --add-device` again."
        )
    added = {d.device_id for d in outcome.devices}
    merged = (
        *(d for d in current.devices if d.device_id not in added),
        *outcome.devices,
    )
    dropped, kept = merged[:-DEVICES_MAX], merged[-DEVICES_MAX:]
    try:
        linkfiles.save_link(replace(current, devices=kept))
    except OSError as exc:
        _fail(
            f"magent link: could not save {linkfiles.LINK_PATH} "
            f"({type(exc).__name__}); the device is paired on the relay but not "
            "trusted here. Remove it in the app, then pair it again."
        )
    for device in dropped:
        click.echo(
            f"Forgot {_shown(device.name)}: this machine trusts at most "
            f"{DEVICES_MAX} devices."
        )


def _pair(
    config_file: Path,
    existing: LinkFile | None,
    remote_control: bool | None,
    no_qr: bool,
) -> None:
    """Pair a new host (``existing`` None) or add a device to this one."""
    from magent import link  # heavy subsystem: in-body per policy

    keys = _keys_for(existing)
    relay = existing.relay_url if existing is not None else link.relay_url()
    try:
        client = link.RelayClient(relay)
    except ValueError:
        _fail(
            "magent link: the relay URL is not usable; it needs to be "
            "http(s)://host[:port] (MAGENT_LINK_URL)."
        )
    outcome = _claim(client, keys, existing, no_qr)
    if existing is None:
        _save_new_host(client, keys, outcome)
    else:
        _save_added_device(existing, outcome)
    how = "QR verified" if outcome.via_qr else "safety number confirmed"
    for device in outcome.devices:
        click.echo(f"Paired {_shown(device.name)} ({how}).")
    if remote_control is None and existing is None:
        remote_control = click.confirm(REMOTE_CONTROL_PROMPT, default=False)
    if remote_control is not None:
        _set_remote_control(config_file, remote_control)
    click.echo(f"This machine is linked as {_shown(outcome.host_id)}.")
    click.echo("`magent serve` keeps the link open; start it if it is not running.")


@main.command("link")
@click.option(
    "--remote-control/--no-remote-control",
    "remote_control",
    default=None,
    help="Let paired devices send text, choose, interrupt, change model, "
    "start/stop sessions and edit projects (asked when not given; default off).",
)
@click.option(
    "--add-device", is_flag=True, help="Pair one more device with this linked machine."
)
@click.option(
    "--no-qr", is_flag=True, help="Print the pairing link instead of a QR code."
)
@click.option(
    "--status", "show_status", is_flag=True, help="Show the link and its devices."
)
@click.option("--json", "as_json", is_flag=True, help="With --status: print JSON.")
@click.pass_context
def link_cmd(
    ctx: click.Context,
    remote_control: bool | None,
    add_device: bool,
    no_qr: bool,
    show_status: bool,
    as_json: bool,
) -> None:
    """Pair this machine with the magent app over the relay."""
    from magent import linkfiles  # heavy subsystem: in-body per policy

    config_file = find_config(ctx.obj.get("config_path"))
    if show_status:
        _show_status(config_file, as_json)
        return
    if as_json:
        raise click.UsageError("--json goes with --status")
    try:
        existing = linkfiles.load_link()
    except linkfiles.LinkFileError as exc:
        _fail(f"{exc}. Run `magent unlink`, then `magent link`.")
    state = linkfiles.load_state()
    revoked = (
        existing is not None
        and state.state == "revoked"
        and state.host_id in (None, existing.host_id)
    )
    if existing is not None and not revoked and not add_device:
        if remote_control is not None:
            _set_remote_control(config_file, remote_control)
            return
        from magent import link  # heavy subsystem: in-body per policy

        click.echo(
            f"This machine is already linked (host {_shown(existing.host_id)} on "
            f"{link.shown_url(existing.relay_url)})."
        )
        click.echo(
            "Pair another device with `magent link --add-device`, "
            "or start over with `magent unlink`."
        )
        return
    if add_device and (existing is None or revoked):
        _fail("This machine is not linked yet; run `magent link` first.")
    _pair(config_file, existing if add_device else None, remote_control, no_qr)


@main.command("unlink")
def unlink_cmd() -> None:
    """Forget this machine's link and ask the relay to revoke it."""
    from magent import link, linkfiles  # heavy subsystem: in-body per policy

    try:
        existing = linkfiles.load_link()
    except linkfiles.LinkFileError as exc:
        linkfiles.delete_link()
        link.sweep_incoming_if_free()
        click.echo(
            f"Removed an unreadable link file ({exc}). If the app still lists "
            "this machine, revoke it from the app's Hosts page."
        )
        return
    if existing is None:
        click.echo("This machine is not linked.")
        return
    problem: str | None = None
    try:
        link.RelayClient(
            existing.relay_url, token=existing.host_token, timeout=RELEASE_TIMEOUT_S
        ).call("DELETE", "/v1/host", retries=0)
    except link.Revoked:
        problem = None  # already revoked on the relay: nothing left to tell it
    except link.RelayError as exc:
        problem = _shown(str(exc), 300)
    except ValueError:
        problem = "link.json's relay URL or host token cannot be used"
    linkfiles.delete_link()
    # Left-over pulls hold decrypted files; a serve still running the link
    # may own one, so they wait for the next serve that takes the link.
    link.sweep_incoming_if_free()
    if problem is None:
        click.echo("Unlinked. The relay revoked this machine.")
    else:
        click.echo(
            f"Unlinked here, but the relay could not be told ({problem}). "
            "Revoke this machine from the app's Hosts page."
        )
