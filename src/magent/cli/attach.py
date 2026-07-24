"""SSH/attach orchestration: the remote-PC attach flow (`_attach_flow`,
radon D/29 -- relocated unchanged per E6.md S2.5), its no-mux sibling, and
the `up`/`attach`/`hotkey` commands. Carries E4's supports_hotkey() gates
(hotkey_cmd, _attach_flow) verbatim.
"""

from __future__ import annotations

import getpass
import json
import subprocess
import sys
import time
from collections import Counter

import click

from magent.cli.app import main
from magent.cli.background import _maybe_start_hotkey, _maybe_start_upload_server
from magent.cli.config_io import (
    _as_dict,
    _as_str,
    _load_config_or_exit,
    _project_dicts,
)
from magent.cli.ui import _banner, _divider, _print_session_overview
from magent.config import DEFAULT_TOOLS
from magent.grid import compute_grid
from magent.log import get_logger
from magent.paths import find_config
from magent.style import style
from magent.tiling import Placement, place_windows
from magent.titles import make_title, parse_title


def _as_session_list(raw: list[object]) -> list[dict[str, object]]:
    """Narrow a JSON list of unknown objects to a list of string-keyed dicts."""
    return [item for item in raw if isinstance(item, dict)]  # ty: ignore[invalid-return-type]  # reason: isinstance(item, dict) narrows; ty 0.0.56 invariance gap


def _default_attach_host() -> str | None:
    """Best-guess SSH target from the local config's project ``host`` fields."""
    try:
        data = _as_dict(json.loads(find_config(None).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    hosts = [h for p in _project_dicts(data) if (h := _as_str(p.get("host")))]
    if not hosts:
        return None
    return Counter(hosts).most_common(1)[0][0]


def _split_target(host: str) -> tuple[str, str]:
    if "@" in host:
        user, hostname = host.split("@", 1)
        return user, hostname
    return getpass.getuser(), host


def _ssh_capture(
    target: str, remote_cmd: str, timeout: int = 30
) -> tuple[int, str, str]:
    """Run a single non-interactive SSH command, returning (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                target,
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "ssh timed out"
    except FileNotFoundError:
        return 127, "", "ssh not found on PATH"
    else:
        return r.returncode, r.stdout, r.stderr


def _ssh_json(
    target: str, remote_cmd: str, timeout: int = 30
) -> dict[str, object] | None:
    """Run a remote command and parse its last single-line JSON object (skips banners)."""
    _, out, _ = _ssh_capture(target, remote_cmd, timeout)
    for line in reversed([ln.strip() for ln in out.splitlines() if ln.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _tile_titles(titles: list[str]) -> None:
    """Tile already-opened windows into the monitor grid. magent:-grammar titles
    are matched by parsed name (badge-proof); anything else falls back to an
    exact-title match."""
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    plat = get_platform()
    plat.set_dpi_aware()
    monitors = plat.list_monitors()
    if not monitors:
        get_logger("launch").error("no monitors detected; windows opened but not tiled")
        click.echo(
            f"  {style('!', fg='yellow')} No monitors detected; windows opened but not tiled."
        )
        return
    slots = compute_grid(monitors, 2, 1)

    click.echo(f"\n  {style('#', fg='cyan')} Tiling {len(titles)} window(s)...")
    placements = []
    for i, title in enumerate(titles):
        parsed = parse_title(title)
        key, mode = (
            (parsed[0], "magent-name") if parsed is not None else (title, "exact")
        )
        placements.append(
            Placement(name=title, key=key, mode=mode, slot=slots[i % len(slots)])
        )
    place_windows(
        plat,
        placements,
        settle_s=3,
        on_placed=lambda p: click.echo(f"    {style('+', fg='green')} {p.name}"),
        on_missing=lambda p: click.echo(
            f"    {style('x', fg='red')} {p.name} {style('not found', dim=True)}"
        ),
    )


def _bring_up_and_requery(
    target: str, grp_suffix: str, fallback_up: list[dict[str, object]]
) -> list[dict[str, object]]:
    click.echo(
        f"  {style('o', fg='cyan')} starting sessions on host (this can take a moment)..."
    )
    rc, _, err = _ssh_capture(target, f"magent up{grp_suffix}", timeout=300)
    if rc != 0:
        click.echo(
            f"  {style('!', fg='yellow')} bring-up exited {rc}: {style(err.strip()[:200], dim=True)}"
        )
    time.sleep(1)
    new = _ssh_json(target, f"magent up --json{grp_suffix}", timeout=30)
    if not new:
        return fallback_up
    raw_up = new.get("up")
    return _as_session_list(raw_up) if isinstance(raw_up, list) else fallback_up  # ty: ignore[invalid-argument-type]  # reason: isinstance(raw_up, list) proves list; ty 0.0.56 invariance gap


def _attach_flow(
    host: str | None, no_mux: bool = False, group: str | None = None, yes: bool = False
) -> None:
    """Remote-PC attach: bring the host's sessions up, then open local windows.

    Default (psmux): tile one local window per remote psmux session and run the
    Alt+V image hotkey. ``--no-mux``: open one plain SSH window per project that
    runs the agent directly (no multiplexer). ``group`` limits the whole flow to
    one project group on the host; ``yes`` skips the bring-up prompt.
    """

    grp = f' -g "{group}"' if group else ""

    if not host:
        default = _default_attach_host()
        host = click.prompt(
            f"  {style('SSH host', fg='cyan')} {style('(user@host -- blank uses config)', dim=True)}",
            default=default or "",
            show_default=bool(default),
        ).strip()
    if not host:
        click.echo(f"  {style('x', fg='red')} No host provided.")
        sys.exit(1)

    user, hostname = _split_target(host)
    target = f"{user}@{hostname}"

    _banner()
    mode_tag = style("[no-mux]", fg="yellow") if no_mux else style("[psmux]", fg="cyan")
    grp_tag = f"  {style(f'group={group}', fg='cyan')}" if group else ""
    click.echo(
        f"  {style('Attach', bold=True)}  {style(f'-> {target}', dim=True)}  {mode_tag}{grp_tag}"
    )
    _divider()
    click.echo()

    click.echo(f"  {style('Querying projects on host...', dim=True)}")
    status = _ssh_json(target, f"magent up --json{grp}", timeout=30)
    if status is None:
        rc, _, _ = _ssh_capture(target, "magent --version")
        click.echo(
            f"\n  {style('x', fg='red')} Could not read project status from {target}."
        )
        if rc != 0:
            click.echo(
                f"  {style('Is magent installed and on PATH on the host?', dim=True)}"
            )
        sys.exit(1)
    if status.get("error"):
        click.echo(f"\n  {style('x', fg='red')} Host error: {status['error']}")
        sys.exit(1)
    if not status.get("projects"):
        where = f" in group '{group}'" if group else ""
        click.echo(
            f"\n  {style('x', fg='red')} No eligible projects{where} on the host."
        )
        sys.exit(1)

    if no_mux:
        _attach_nomux(target, status)
        return

    raw_up = status.get("up")
    raw_down = status.get("down")
    up = _as_session_list(raw_up) if isinstance(raw_up, list) else []  # ty: ignore[invalid-argument-type]  # reason: isinstance guard; ty 0.0.56 invariance gap
    down = _as_session_list(raw_down) if isinstance(raw_down, list) else []  # ty: ignore[invalid-argument-type]  # reason: isinstance guard; ty 0.0.56 invariance gap
    port = status.get("upload_port", 8033)

    if down and yes:
        up = _bring_up_and_requery(target, grp, up)
    elif down:
        pickable = _print_session_overview(hostname, up, down)
        opts = [f"{style('a', fg='cyan', bold=True)}=all {len(down)}"]
        if pickable:
            opts.append(
                f"{style('1-' + str(len(pickable)), fg='cyan', bold=True)}=one group"
            )
        opts.append(f"{style('n', fg='cyan', bold=True)}=none")
        click.echo(f"  {style('Bring up', bold=True)}   " + "   ".join(opts))
        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="a",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )

        if choice in ("n", "no", "none", "q"):
            pass
        elif choice in ("a", "y", "all", ""):
            up = _bring_up_and_requery(target, grp, up)
        else:
            sel = None
            if choice.isdigit() and 1 <= int(choice) <= len(pickable):
                sel = pickable[int(choice) - 1]
            else:
                sel = next((g for g in pickable if g.lower() == choice), None)
            if sel:
                up = _bring_up_and_requery(target, f' -g "{sel}"', up)
            else:
                click.echo(
                    f"  {style('?', fg='yellow')} unrecognized choice -- bringing up none."
                )

    if not up:
        click.echo(f"\n  {style('x', fg='red')} No sessions are up on the host.")
        sys.exit(1)

    titles: list[str] = []
    for sess in up:
        # The psmux socket id (P3-01): drives the window title, the wire the
        # Alt+V hotkey posts back, and the host-side `magent sessions <id>`.
        sid = _as_str(sess.get("session")) or _as_str(sess.get("name"))
        title = make_title(sid)
        click.echo(f"  {style('o', fg='cyan')} {title}")
        subprocess.Popen(
            [
                "wt",
                "-w",
                "new",
                "--title",
                title,
                "--suppressApplicationTitle",
                "--",
                "ssh",
                "-t",
                target,
                f"magent sessions {sid}",
            ]
        )
        titles.append(title)
        time.sleep(0.4)

    _tile_titles(titles)

    # Guarantee the host runs an upload server for Alt+V -- independent of the
    # host's uploadServer flag and of whether anything was just brought up.
    rc, _, _ = _ssh_capture(target, f"magent serve -p {port} --ensure", timeout=15)
    if rc != 0:
        click.echo(
            f"  {style('!', fg='yellow')} couldn't confirm an upload server on the host"
            f" {style('-- Alt+V may not work', dim=True)}"
        )

    server_url = f"http://{hostname}:{port}"
    click.echo(
        f"\n  {style('#', fg='magenta')} Hotkey {style('Alt+V', bold=True)} pastes clipboard images"
        f" {style('(only in magent: windows)', dim=True)} {style('->', dim=True)} {style(server_url, fg='cyan')}"
    )
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if get_platform().supports_hotkey():
        pid = _maybe_start_hotkey(server_url)
        if pid:
            click.echo(
                f"  {style('+', fg='green')} Alt+V listener running in the background "
                f"{style(f'(pid {pid})', dim=True)}"
            )
            click.echo(
                f"  {style('Progress shows in each magent: window. Stop with', dim=True)} "
                f"{style('magent down --all', bold=True)}{style('.', dim=True)}"
            )
        else:
            click.echo(f"  {style('!', fg='yellow')} couldn't start the Alt+V listener")


def _attach_nomux(target: str, status: dict[str, object]) -> None:
    """Open one plain SSH window per project, running the agent directly (no psmux)."""

    projects = _project_dicts(status)
    if not projects:
        click.echo(f"  {style('x', fg='red')} No eligible projects in the host config.")
        sys.exit(1)

    click.echo(
        f"  {style(str(len(projects)), fg='green', bold=True)} project(s) "
        f"{style('-- direct SSH, no multiplexer', dim=True)}\n"
    )

    titles: list[str] = []
    for p in projects:
        # Window title = psmux socket id so the Alt+V hotkey resolves it (P3-01).
        title = make_title(_as_str(p.get("session")) or _as_str(p.get("name")))
        remote_dir = _as_str(p.get("resolved")) or _as_str(p.get("path"))
        # NF-S3-004: fall back to the registry default, never a drifting literal.
        cmd = _as_str(p.get("cmd")) or DEFAULT_TOOLS["claude"]
        click.echo(f"  {style('o', fg='cyan')} {title}")
        subprocess.Popen(
            [
                "wt",
                "-w",
                "new",
                "--title",
                title,
                "--suppressApplicationTitle",
                "--",
                "ssh",
                "-t",
                target,
                f"cd {remote_dir} && {cmd}",
            ]
        )
        titles.append(title)
        time.sleep(0.4)

    _tile_titles(titles)
    click.echo(
        f"\n  {style('Done.', fg='green', bold=True)} "
        f"{style('(no-mux mode: Alt+V image paste is not available)', dim=True)}"
    )


@main.command("up")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print session status as JSON without changing anything",
)
@click.option(
    "--all",
    "do_all",
    is_flag=True,
    help="Recreate every session, not just the ones that are down",
)
@click.option(
    "-g", "--group", default=None, help="Only projects tagged with this group"
)
@click.pass_context
def up_cmd(ctx: click.Context, as_json: bool, do_all: bool, group: str | None) -> None:
    """Ensure a persistent psmux session per project (host side of `attach`)."""
    config_file = find_config(ctx.obj.get("config_path"))
    # Shared as_json config-error envelope (NF-S3-005): --json always gets JSON,
    # never a stderr Error: line. Folds the former up_cmd raw-loader exception.
    cfg = _load_config_or_exit(config_file, as_json=as_json)

    from magent.launch import (  # heavy subsystem: in-body per policy
        bring_up_psmux,
        psmux_status,
    )

    up, down, projects = psmux_status(cfg, group=group)

    if as_json:
        click.echo(
            json.dumps(
                {
                    # P3-03: snake_case across all CLI JSON; P3-04: ok-envelope.
                    "ok": True,
                    "platform": sys.platform,
                    "psmux": cfg.settings.psmux,
                    "upload_server": cfg.settings.upload_server,
                    "upload_port": cfg.settings.upload_port,
                    # up/down entries already carry name (display) + session
                    # (psmux socket id) from psmux_status (P3-01).
                    "up": up,
                    "down": down,
                    "projects": [
                        {
                            "name": p["name"],
                            "session": p["session"],
                            "path": p["path"],
                            "tool": p["tool"],
                            "group": p["group"],
                            "resolved": p["resolved"],
                            "cmd": p["cmd"],
                        }
                        for p in projects
                    ],
                }
            )
        )
        return

    _banner()
    click.echo(
        f"  {style('Bring up sessions', bold=True)}  {style(str(config_file), dim=True)}"
    )
    _divider()
    click.echo()

    targets = (
        None
        if do_all
        else [_as_str(d.get("session")) or _as_str(d.get("name")) for d in down]
    )
    if not projects:
        where = f" in group '{group}'" if group else ""
        click.echo(f"  {style('!', fg='yellow')} No eligible projects{where}.")
    elif not do_all and not down:
        click.echo(f"  {style('+', fg='green')} All {len(up)} session(s) already up.")
    else:
        created = bring_up_psmux(cfg, only=targets, group=group)
        click.echo(
            f"  {style('+', fg='green')} Brought up {style(str(len(created)), fg='green', bold=True)}"
            f" session(s): {style(', '.join(created) or '(none)', dim=True)}"
        )

    if cfg.settings.upload_server:
        _maybe_start_upload_server(cfg.settings.upload_port, str(config_file))
        click.echo(
            f"  {style('#', fg='magenta')} upload server on port {style(str(cfg.settings.upload_port), fg='cyan')}"
        )


@main.command("attach")
@click.argument("host", required=False)
@click.option(
    "--no-mux", is_flag=True, help="One plain SSH window per project (no psmux/tmux)"
)
@click.option(
    "-g", "--group", default=None, help="Only attach/bring up projects in this group"
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip the bring-up prompt (bring up everything that's down)",
)
@click.pass_context
def attach_cmd(
    ctx: click.Context, host: str | None, no_mux: bool, group: str | None, yes: bool
) -> None:
    """Attach to another machine's magent sessions over SSH.

    HOST is user@host (omit to be prompted; blank uses the host from your local
    config). Default tiles one window per remote psmux session with Alt+V image
    paste; --no-mux opens a direct SSH window per project instead. -g limits the
    flow to one project group on the host; -y skips the bring-up prompt.
    """
    _attach_flow(host, no_mux=no_mux, group=group, yes=yes)


@main.command("hotkey")
@click.option(
    "--server", "-s", default="http://localhost:8033", help="Upload server URL"
)
@click.pass_context
def hotkey_cmd(ctx: click.Context, server: str) -> None:
    """Listen for Alt+V to upload clipboard images to psmux sessions.

    Only activates when a 'magent:' titled window is focused. Otherwise
    the keystroke passes through normally.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_hotkey():
        click.echo(f"  {style('x', fg='red')} Hotkey listener is Windows-only.")
        sys.exit(1)

    from magent.hotkey import (
        listener_pid,  # ImportError off-Windows (hotkey.py guards); must stay lazy
    )

    existing = listener_pid()
    if existing:
        click.echo(
            f"  {style('!', fg='yellow')} An Alt+V listener is already running "
            f"{style(f'(pid {existing})', dim=True)}."
        )
        click.echo(
            f"  {style('Stop it first with', dim=True)} {style('magent down --all', bold=True)}{style('.', dim=True)}"
        )
        return

    _banner()
    click.echo(
        f"  {style('Hotkey listener', bold=True)}  {style(f'-> {server}', dim=True)}"
    )
    _divider()
    click.echo()
    click.echo(
        f"  {style('Alt+V', fg='cyan', bold=True)} uploads clipboard image to the focused project"
    )
    click.echo(f"  {style('Only active in windows titled magent:<project>', dim=True)}")
    click.echo(f"  {style('Ctrl+C to stop.', dim=True)}")
    click.echo()

    from magent.hotkey import (
        run_hotkey,  # ImportError off-Windows (hotkey.py guards); must stay lazy
    )

    try:
        run_hotkey(server)
    except KeyboardInterrupt:
        click.echo(f"\n  {style('Stopped.', dim=True)}")
    except RuntimeError as e:
        click.echo(f"  {style('x', fg='red')} {e}")
        sys.exit(1)
