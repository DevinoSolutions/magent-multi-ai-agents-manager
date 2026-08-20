"""SSH/attach orchestration: the remote-PC attach flow (`_attach_flow`,
radon D/29 -- relocated unchanged per E6.md S2.5), its no-mux sibling, and
the `up`/`attach`/`hotkey` commands. Carries E4's supports_hotkey() gates
(hotkey_cmd, _attach_flow) verbatim.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import click

from magent.attach_client import (
    CLIENT_EXE_NAME,
    SSH_CONNECTION_OPTS,
    remote_attach_command,
)
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
from magent.tiling import (
    RETRY_SECS_CONTAINS,
    Placement,
    find_in_snapshot,
    place_windows,
    window_open,
)
from magent.titles import make_title, parse_title

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from magent.platform import Platform


def _as_session_list(raw: list[object]) -> list[dict[str, object]]:
    """Narrow a JSON list of unknown objects to a list of string-keyed dicts."""
    return [item for item in raw if isinstance(item, dict)]


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


_LAST_HOST_FILE = Path.home() / ".magent" / "last-attach-host"


def _read_last_host() -> str | None:
    """The target of the most recent successful attach, if any."""
    try:
        text = _LAST_HOST_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _remember_last_host(target: str) -> None:
    """Persist the target once the host has answered a status query, so the
    next no-argument ``magent attach`` offers it as the prompt default.
    Best-effort: a read-only home dir must not break the attach itself."""
    with contextlib.suppress(OSError):
        _LAST_HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_HOST_FILE.write_text(target, encoding="utf-8")


def _split_target(host: str) -> tuple[str, str]:
    if "@" in host:
        user, hostname = host.split("@", 1)
        return user, hostname
    return getpass.getuser(), host


def _ssh_capture(
    target: str, remote_cmd: str, timeout: int = 30, stdin_text: str | None = None
) -> tuple[int, str, str]:
    """Run a single non-interactive SSH command, returning (rc, stdout, stderr).

    ``stdin_text`` feeds the remote command on stdin (``magent config put``
    reads a whole config that way). Keeping it here rather than in a second
    helper keeps one entry point for every SSH invocation magent makes, so the
    BatchMode/ConnectTimeout posture can never drift between them."""
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
            input=stdin_text,
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


def _last_json_obj(out: str) -> dict[str, object] | None:
    """Parse the last single-line JSON object out of command output (skips banners)."""
    for line in reversed([ln.strip() for ln in out.splitlines() if ln.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _ssh_json(
    target: str, remote_cmd: str, timeout: int = 30
) -> dict[str, object] | None:
    """Run a remote command and parse its last single-line JSON object."""
    _, out, _ = _ssh_capture(target, remote_cmd, timeout)
    return _last_json_obj(out)


# A just-booted host can spend far longer than the steady-state budget on its
# first `magent up --json` (cold interpreter, cloud-synced project dirs), so a
# timeout gets one retry with a much longer leash before we give up.
_STATUS_TIMEOUT_S = 30
_STATUS_RETRY_TIMEOUT_S = 120


def _query_status(
    target: str, grp_suffix: str
) -> tuple[dict[str, object] | None, int, str]:
    """Fetch the host's `magent up --json`, returning (status, ssh rc, stderr)
    so the caller can say WHY when the read fails instead of a generic error."""
    # Revive dead-agent panes while we're reading status, so a session that is
    # "up" but parked at a bare shell gets its agent back before we open a
    # window onto it. A host running an older magent rejects the unknown flag
    # with nothing on stdout, and the `||` falls back to the plain read -- the
    # same trick as the `psmux attach || magent sessions` line further down.
    cmd = f"magent up --json --revive{grp_suffix} || magent up --json{grp_suffix}"
    rc, out, err = _ssh_capture(target, cmd, timeout=_STATUS_TIMEOUT_S)
    if rc == 124:
        click.echo(
            f"  {style('o', fg='yellow')} host is slow to answer -- retrying"
            f" {style(f'(up to {_STATUS_RETRY_TIMEOUT_S}s)', dim=True)}..."
        )
        rc, out, err = _ssh_capture(target, cmd, timeout=_STATUS_RETRY_TIMEOUT_S)
    return _last_json_obj(out), rc, err


def _warn_version_skew(target: str, status: dict[str, object]) -> None:
    """Say so, loudly but harmlessly, when the host runs a different magent.

    `up --json` grew a `version` key in this release, so a MISSING key means
    the host predates it -- that is the exact situation this warns about (a
    laptop on a new magent silently losing status hints / --revive / the F2
    folder lookup because the host never implemented them). Purely advisory:
    printed by the attach CLIENT on stderr so `--json` consumers upstream are
    untouched, and never fatal -- attach carries on either way.
    """
    # deferred: resolving __version__ costs an importlib.metadata import, and
    # only this one diagnostic needs it (see cli/ui.py::_banner).
    from magent import __version__

    host_version = _as_str(status.get("version"))
    if host_version == __version__:
        return
    running = f"magent {host_version}" if host_version else "an older magent"
    click.echo(
        f"\n  {style('!', fg='yellow')} {style(f'{target} runs {running}', fg='yellow')}"
        f"{style(f'; this machine runs magent {__version__}.', fg='yellow')}",
        err=True,
    )
    click.echo(
        f"  {style('Status-line hints, --revive and the F2 folder lookup may be unavailable.', fg='yellow')}",
        err=True,
    )
    click.echo(
        f"  {style('Upgrade the host with', fg='yellow')}"
        f" {style('pip install -U magent-multi-ai-agents-manager', fg='yellow', bold=True)}",
        err=True,
    )


def _explain_status_failure(target: str, rc: int, err: str) -> None:
    """One diagnostic line for a failed status read: timeout, ssh error, or
    missing-magent -- previously all three collapsed into the same message."""
    click.echo(
        f"\n  {style('x', fg='red')} Could not read project status from {target}."
    )
    detail = err.strip().splitlines()[-1] if err.strip() else ""
    if rc == 124:
        click.echo(
            f"  {style('SSH timed out -- the host may still be starting up; try again in a minute.', dim=True)}"
        )
        return
    if rc != 0:
        if detail:
            click.echo(f"  {style(f'ssh exited {rc}: {detail[:200]}', dim=True)}")
        if "not recognized" in err or "not found" in err:
            click.echo(
                f"  {style('Is magent installed and on PATH on the host?', dim=True)}"
            )
        return
    vrc, _, _ = _ssh_capture(target, "magent --version")
    if vrc != 0:
        click.echo(
            f"  {style('Is magent installed and on PATH on the host?', dim=True)}"
        )


def _local_grid() -> tuple[int, int]:
    """The attaching machine's configured window grid (columns, rows), falling
    back to 2x1 -- attach used to hardcode 2x1 and ignore the local layout."""
    try:
        data = _as_dict(json.loads(find_config(None).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 2, 1
    layout = _as_dict(data.get("layout"))
    from magent.cli.config_io import _as_int  # narrow raw-dict helper

    return max(1, _as_int(layout.get("columns"), 2)), max(
        1, _as_int(layout.get("rows"), 1)
    )


def _match_key(title: str) -> tuple[str, str]:
    """How tiling should look this window up: magent:-grammar titles match on
    the parsed name (badge-proof), anything else on the exact title."""
    parsed = parse_title(title)
    return (parsed[0], "magent-name") if parsed is not None else (title, "exact")


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
    cols, rows = _local_grid()
    slots = compute_grid(monitors, cols, rows)

    click.echo(f"\n  {style('#', fg='cyan')} Tiling {len(titles)} window(s)...")
    placements = []
    for i, title in enumerate(titles):
        key, mode = _match_key(title)
        placements.append(
            Placement(name=title, key=key, mode=mode, slot=slots[i % len(slots)])
        )
    # A big remote attach spawns windows over SSH for tens of seconds, so
    # latecomers need a deadline that scales with the window count -- but
    # polling from t=0 means windows that are already open tile immediately
    # instead of waiting out a fixed settle. That settle was the "awkward
    # finite wait" bug: a 40-window attach slept 40s before touching anything,
    # even when every session already existed and every window was up in one.
    place_windows(
        plat,
        placements,
        deadline_s=min(120, max(RETRY_SECS_CONTAINS, 2 * len(titles))),
        on_placed=lambda p: click.echo(f"    {style('+', fg='green')} {p.name}"),
        on_missing=lambda p: click.echo(
            f"    {style('x', fg='red')} {p.name} {style('not found', dim=True)}"
        ),
    )


def _reclaim_geometry(titles: list[str]) -> None:
    """Make every window we just tiled re-assert its size to psmux.

    psmux 3.3.6 arbitrates nothing: a session renders at whatever geometry the
    LAST client resize-or-attach event reported, and nothing server-side ever
    recomputes it -- detaching or killing the other client does not release its
    geometry, and `refresh-client`, `resize-pane` and `detach-client -a` are
    silent no-ops (`resize-window` is worse: a no-op that HANGS under
    `window-size manual`, so magent must never call it). The symptom is a
    session sized by another machine rendering squeezed into part of this one's
    window, with lines clipped.

    The one event psmux always honors is a CLIENT resize -- which is why the
    manual Ctrl+/Ctrl- zoom trick fixes it: changing the terminal's cell grid
    makes the client report a new size. So we do the same thing without the
    zoom: nudge each OS window's size and put it straight back. Over SSH that
    travels as a real SIGWINCH to the remote psmux client, so the machine
    actually looking at the session wins.

    Runs AFTER tiling so the rect we restore is the final tiled one, and
    covers already-open windows too -- those are exactly the stale case, since
    tiling either skips them or moves them to where they already were, and
    neither fires a resize event.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    plat = get_platform()
    if not plat.supports_window_nudge():
        return
    snap = plat.snapshot_windows()
    log = get_logger("launch")
    handles: list[object] = []
    for title in titles:
        key, mode = _match_key(title)
        handle = find_in_snapshot(snap, key, mode)
        if handle is None:
            # Closed mid-flight, or never appeared (tiling already said so).
            log.warning("geometry nudge: window not found: key=%r mode=%s", key, mode)
            continue
        handles.append(handle)
    if not handles:
        return
    try:
        nudged = plat.nudge_windows(handles)
    except OSError:
        # Best-effort by construction: a window that dies between the snapshot
        # and the resize must not cost the user their attach.
        log.warning("geometry nudge failed", exc_info=True)
        return
    log.info("geometry nudge: %d/%d window(s)", nudged, len(titles))
    if nudged:
        click.echo(
            f"  {style('#', fg='cyan')} Reclaimed terminal geometry"
            f" for {nudged} window(s)."
        )


# Pause between `wt` window spawns. The real constraint is sshd, not the
# terminal: every window opens its own SSH connection, and spawning too fast
# trips MaxStartups (default 10:30:100 -- beyond 10 concurrent *unauthenticated*
# handshakes it starts dropping connections probabilistically). At 4 spawns/sec
# against ~1s handshakes we sit around 4 in flight, well clear of that; the old
# 0.4s was purely conservative and cost 16s on a 40-window attach.
_SPAWN_STAGGER_S = 0.25


def _already_open(names: list[str]) -> set[str]:
    """The subset of ``names`` that already has a local magent: window.

    Attach used to spawn a `wt` window per remote session unconditionally, so
    re-attaching while the previous attach's windows were still open stacked a
    duplicate window on every session. The launch path has always skipped what
    is already up (launch.py's ``_is_running``); this borrows the same matcher
    tiling resolves windows with, so a title carrying a state badge still
    counts as open.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    # set_dpi_aware() stays in _tile_titles: enumerating titles needs no DPI
    # context, and that global side effect should fire exactly once.
    snap = get_platform().snapshot_windows()
    return {name for name in names if window_open(snap, name)}


def _open_attach_sids(snap: dict[str, object]) -> list[str]:
    """Every session name that currently has a local magent: window."""
    return sorted({p[0] for t in snap if (p := parse_title(t)) is not None})


def _partition_open_sids(up_sids: Sequence[str]) -> tuple[set[str], set[str]]:
    """One window snapshot, split into ``(windows for up_sids, everything else)``.

    The second half is what v3.10's corpse machinery never looked at. Its whole
    world was ``_already_open(up_sids)``, so a magent: window whose SESSION is
    also gone -- the host rebooted, the session was killed, or the user answered
    ``n`` / picked one group at the bring-up prompt -- was never scanned, never
    closed and never flagged: a terminated pane sitting in the grid forever.

    Empty names are dropped from both halves: a degenerate bare ``magent:``
    title is nobody's session and must not become a close target.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    snap = get_platform().snapshot_windows()
    open_all = {sid for sid in _open_attach_sids(snap) if sid}
    wanted = {sid for sid in up_sids if sid}
    return open_all & wanted, open_all - wanted


def _echo_already_open(title: str) -> None:
    """The spawn loops' counterpart to their `o <title>` line."""
    click.echo(f"  {style('=', fg='cyan')} {title} {style('already open', dim=True)}")


# The local processes that can be a live attach CLIENT for a psmux session:
#
# * the SSH client the remote path ultimately runs (`ssh -t <target> "psmux -L
#   <sid> attach || ..."`),
# * the psmux client a locally-launched window runs directly
#   (platform/windows.py::attach_psmux), and
# * the reconnect supervisor that now sits between wt and ssh
#   (attach_client.py). This one is load-bearing: while it waits out a backoff
#   there is NO ssh process on the machine, and a scan that only knew about
#   ssh/psmux would score a pane a corpse precisely while it was healing
#   itself -- then close it, and take the reconnect down with it.
#
# All three carry the session's attach command in their command line, which is
# the only thing that distinguishes a live window from a corpse -- the TITLE
# cannot, because attach spawns wt with --suppressApplicationTitle, so the
# window's magent: title survives the process it named.
#
# The supervisor is spawned via its console script, whose Windows launcher stub
# keeps the script's own name (`magent-attach-client.exe`) and the full argv,
# and waits on the python child it starts -- so scanning for the stub finds a
# live supervisor for its whole lifetime, backoff sleeps included.
_CLIENT_PROCESS_NAMES = ["ssh.exe", "psmux.exe", f"{CLIENT_EXE_NAME}.exe"]


def _attach_markers(sid: str) -> tuple[str, ...]:
    """Every spelling of "this process is attached to ``sid``" magent can spawn.

    The binary NAME is deliberately not part of the marker. The remote path
    spawns a literal ``psmux -L <sid> attach`` string inside ssh, but the local
    launch path (platform/windows.py::attach_psmux) execs the *resolved* binary
    -- an absolute path to psmux.exe -- so a ``psmux -L`` prefix would miss it
    and score every locally-launched window a corpse. That never mattered while
    corpse detection only ever saw the remote host's up-sessions; it matters now
    that ``_sweep_dead_windows`` looks at every magent: window on the machine.
    ``_CLIENT_PROCESS_NAMES`` already narrows the scan to attach-client
    processes, so ``-L <sid> attach`` is signal enough on its own.

    Not naming the binary is also what let the reconnect supervisor join the
    scan for free: ``_spawn_windows`` passes the remote command it would have
    given ssh as the supervisor's ``--remote`` argument, so the same marker
    string appears in the supervisor's own command line -- including while it
    is between connections and no ssh process exists at all. The one rule that
    keeps this honest is stated at ``attach_client.remote_attach_command``: the
    remote command has exactly one spelling, and these markers match it.

    The quoted variants cover a session id that a shell (or a future call site)
    chose to quote, so a quoting change cannot silently turn every live window
    into a corpse. They also cover the supervisor argv on any platform whose
    process table re-quotes arguments.
    """
    return (
        f"-L {sid} attach",
        f'-L "{sid}" attach',
        f"-L '{sid}' attach",
    )


def _corpses(open_sids: set[str], live_cmdlines: list[str]) -> set[str]:
    """Which open windows have no live attach client behind them.

    Pure on purpose -- the whole decision is "does any of these command lines
    mention this session's attach command", so it is unit-testable without a
    single real process, and the one risky judgement (close a window) is made
    somewhere a test can pin it.

    Deliberately conservative: a session counts as dead only when NO local
    attach client at all -- ssh, psmux, or the reconnect supervisor -- is
    running its attach command. Falsely closing a live window costs the user
    their session; leaving a corpse costs them one re-run of attach.

    That conservatism is what makes the supervisor safe to add here: widening
    ``_CLIENT_PROCESS_NAMES`` can only ever make FEWER windows look dead, never
    more, so the risky direction of this decision was never widened.
    """
    haystack = [c.lower() for c in live_cmdlines if c]
    return {
        sid
        for sid in open_sids
        if not any(
            marker.lower() in cmdline
            for cmdline in haystack
            for marker in _attach_markers(sid)
        )
    }


def _close_windows(
    plat: Platform, snap: dict[str, object], sids: Iterable[str]
) -> list[str]:
    """Ask each session's magent: window to close; report which ones accepted.

    Resolves handles through the same badge-proof matcher tiling uses, so a
    window carrying a state badge is still found. Best-effort per window: one
    that died between the snapshot and the close is skipped, never fatal.
    """
    closed: list[str] = []
    for sid in sids:
        handle = find_in_snapshot(snap, sid, "magent-name")
        if handle is None:
            continue
        with contextlib.suppress(OSError):
            if plat.close_window(handle):
                closed.append(sid)
    return closed


def _dead_sids(open_sids: set[str]) -> set[str]:
    """Which of ``open_sids`` have a magent: window but no live attach client.

    The read-only half of ``_repair_corpses``: same capability gates, same
    conservative "a scan we could not run saw nothing" posture, but it closes
    nothing. The post-retry verification uses it on its own so a window that is
    STILL dead after two spawn attempts is reported rather than closed --
    closing it would leave the user with neither a session nor the pane whose
    error text says why.

    Gated on window-close too, not just process-scan, so "can this client
    repair corpses at all" is one question with one answer: a platform without
    both capabilities skips corpse detection entirely, as it always has.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not open_sids:
        return set()
    plat = get_platform()
    if not (plat.supports_process_scan() and plat.supports_window_close()):
        return set()
    try:
        cmdlines = plat.process_cmdlines(_CLIENT_PROCESS_NAMES)
    except OSError:
        # "We could not look" is not "nothing is running": acting on a failed
        # scan would close every live window at once. Leave them all alone.
        get_logger("launch").warning(
            "attach: could not scan attach clients", exc_info=True
        )
        return set()
    return _corpses(open_sids, cmdlines)


# The two fates a dead magent: window can meet, as the user reads them.
# A corpse whose session is still up gets reopened by the spawn loop; one whose
# session is gone is closed for good, because there is nothing to attach to.
_NOTE_REOPEN = "dead window closed -- reopening"
_NOTE_STALE = "dead window closed (session is not up)"


def _close_and_echo(
    plat: Platform, snap: dict[str, object], sids: Sequence[str], note: str
) -> list[str]:
    """Close each window and print one `~ magent:<sid> <note>` line per success.

    Shared by the two close sites so the corpse-repair and stale-sweep paths
    cannot drift in how they resolve, close or report a window -- only in what
    they say about it.
    """
    closed = _close_windows(plat, snap, sids)
    for sid in closed:
        click.echo(
            f"  {style('~', fg='yellow')} {make_title(sid)} {style(note, dim=True)}"
        )
    return closed


def _repair_corpses(open_sids: set[str]) -> set[str]:
    """Close every open magent: window whose attach client is dead.

    Returns the sessions freed, so the caller can drop them from its
    "already open" set and let the normal spawn loop reopen them.

    The bug this exists for: an attach window's SSH connection dies
    (`client_loop: send disconnect: Connection reset`) and wt keeps the pane
    open showing `[process exited with code 255]`. Because the title never
    changes, ``_already_open`` counted that corpse as a live window and every
    later `magent attach` skipped the session forever.

    Since the reconnect supervisor (attach_client.py) that is no longer the
    COMMON case -- a dropped connection is healed in the pane, seconds later,
    without anyone running attach again. What still lands here is the residue:
    a pane whose supervisor never started (missing/blocked binary), one the
    user stopped with Ctrl+C, one that stopped because the host answered but
    the session was gone, and every pane spawned by an older magent or with
    ``--no-reconnect``. Those are real corpses, and closing them is still the
    right call.

    Scoped to sessions the caller knows are UP -- the post-tiling verification
    pass, which only ever asks about the windows it just spawned. The start-of
    attach sweep is ``_sweep_dead_windows``, which covers the other kind too.

    Capability-gated on both halves (scan + close) via ``_dead_sids``, so
    macOS/Linux clients take the ABC defaults and simply keep today's
    title-only dedupe.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    dead = _dead_sids(open_sids)
    if not dead:
        return set()
    plat = get_platform()
    closed = _close_and_echo(plat, plat.snapshot_windows(), sorted(dead), _NOTE_REOPEN)
    get_logger("launch").info(
        "attach: closed %d dead window(s): %s", len(closed), ", ".join(closed)
    )
    return set(closed)


def _sweep_dead_windows(up_sids: Sequence[str]) -> set[str]:
    """Close EVERY dead local magent: window; return the up-session ones left live.

    Two kinds of corpse, one pass:

    * a window for a session that IS up -- ``_repair_corpses``' case. Closed,
      and dropped from the returned set so the spawn loop reopens it.
    * a window for a session that is NOT in ``up_sids``. Closed and left closed:
      the session died with a previous attach (host rebooted, session killed, or
      the user answered ``n`` / picked one group at the bring-up prompt), so
      there is nothing on the host to attach it back to. v3.10 never looked
      here at all -- it built its candidate set from the up list -- which is why
      a terminated terminal survived every subsequent `magent attach`.

    A LIVE window outside ``up_sids`` is left strictly alone. Under ``-g
    <group>`` the host's up/down lists are group-filtered, so another group's
    perfectly healthy windows are "not up" from this run's point of view;
    liveness, never list membership, is what decides. That is also why the
    stale half runs the SAME ``_dead_sids`` check as the up half rather than
    anything looser.

    Scan economy: one window snapshot to partition, one ``_dead_sids`` process
    scan for both halves, one snapshot to close from (fresh, because the scan
    sits between). Capability-gated through ``_dead_sids``: a platform without
    process-scan + window-close closes nothing and prints nothing, exactly as
    before.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    open_up, open_stale = _partition_open_sids(up_sids)
    dead = _dead_sids(open_up | open_stale)
    if not dead:
        return open_up
    plat = get_platform()
    snap = plat.snapshot_windows()
    log = get_logger("launch")
    reopened = _close_and_echo(plat, snap, sorted(dead & open_up), _NOTE_REOPEN)
    stale = _close_and_echo(plat, snap, sorted(dead & open_stale), _NOTE_STALE)
    if reopened:
        log.info(
            "attach: closed %d dead window(s): %s", len(reopened), ", ".join(reopened)
        )
    if stale:
        log.info(
            "attach: closed %d dead window(s) with no live session: %s",
            len(stale),
            ", ".join(stale),
        )
    return open_up - set(reopened)


def _annotate_dead_windows(up: Sequence[dict[str, object]]) -> None:
    """Say, right under the session overview, which local magent: windows are
    DEAD -- both the ones a "ready" host session is hiding and the ones whose
    session is gone entirely.

    "Dead" means no attach client of any kind is running the session's attach
    command. A pane merely waiting out a reconnect backoff is NOT dead: its
    supervisor is live and carries the marker, so it is never listed here (and
    never closed below). What this reports is a pane nothing is driving.

    The overview is pure host truth: a psmux session that exists on the host
    renders as a green ``N/N ready`` row even when the pane here is a corpse
    whose SSH dropped, and a corpse whose session is NOT up does not appear in
    the overview at all. On a 40-window attach that is exactly the misleading
    case -- the user reads "41 up" and sees several panes stuck on
    `[process exited with code 255]`.

    Annotating at the attach call site (rather than teaching
    ``_print_session_overview`` about it) keeps that renderer shared with the
    host-side `up` flows, where "the local window" has no meaning at all.

    Read-only and capability-gated through ``_dead_sids``: nothing is closed
    here. The real repair happens in the spawn phase off its own FRESH scan,
    because a bring-up can run for minutes and this snapshot would be stale by
    the time it mattered. At most two lines, and only for the halves that are
    actually non-empty.
    """
    sids = [_as_str(s.get("session")) or _as_str(s.get("name")) for s in up]
    open_up, open_stale = _partition_open_sids(sids)
    # One scan for both halves; the partition happens on the RESULT.
    dead = _dead_sids(open_up | open_stale)
    if not dead:
        return
    reopen = sorted(dead & open_up)
    stale = sorted(dead & open_stale)
    if reopen:
        click.echo(
            f"  {style('!', fg='yellow')} {style(str(len(reopen)), fg='yellow', bold=True)}"
            f" {style('window(s) here are dead (no attach client running)', fg='yellow')}"
            f" {style('-- they will be closed and reopened:', fg='yellow')}"
            f" {style(', '.join(reopen), fg='yellow', bold=True)}"
        )
    if stale:
        click.echo(
            f"  {style('!', fg='yellow')} {style(str(len(stale)), fg='yellow', bold=True)}"
            f" {style('dead window(s) from a previous session (session down)', fg='yellow')}"
            f" {style('-- will be closed:', fg='yellow')}"
            f" {style(', '.join(stale), fg='yellow', bold=True)}"
        )


def _attach_client_exe() -> str | None:
    """The local ``magent-attach-client`` binary, or None if it is not on PATH.

    Never assumed present: an editable checkout that predates the console
    script, a PATH that exposes ``magent`` from somewhere its siblings are not,
    or a partially-upgraded install all reach here. The caller degrades to a
    bare ssh pane (today's historical behavior) and says so once, rather than
    spawning forty windows that fail to start.
    """
    return shutil.which(CLIENT_EXE_NAME)


def _pane_command(target: str, sid: str, supervisor: str | None) -> list[str]:
    """What one attach pane runs: the reconnect supervisor, or bare ssh.

    Both spellings drive the SAME ssh options and the SAME remote command
    (``attach_client`` owns both), so the only difference between them is who
    is left standing when the connection drops: with the supervisor the pane
    reconnects itself, without it the pane becomes a corpse for the next
    ``magent attach`` to sweep.

    The supervisor form passes ``--remote`` explicitly rather than letting the
    supervisor derive it: that argument is what puts the ``-L <sid> attach``
    marker into the supervisor's own command line, which is how
    ``_dead_sids`` can tell a pane mid-reconnect from a dead one.
    """
    remote = remote_attach_command(sid)
    if supervisor is None:
        return ["ssh", *SSH_CONNECTION_OPTS, "-t", target, remote]
    return [supervisor, "--target", target, "--session", sid, "--remote", remote]


def _spawn_windows(
    target: str,
    sids: Sequence[str],
    open_already: set[str],
    stagger: float,
    *,
    reconnect: bool = True,
) -> list[str]:
    """Open one `wt` window per session id and return their titles, in order.

    Split out of ``_attach_flow`` so the post-tiling verification pass can call
    it a second time for the windows that died at the SSH handshake -- the
    retry needs the same spawn, only staggered further apart. Both the initial
    and the retry batch therefore get the same pane command from here.

    ``reconnect=False`` (``magent attach --no-reconnect``) reproduces the
    historical bare-ssh pane exactly.
    """
    supervisor = _attach_client_exe() if reconnect else None
    if reconnect and supervisor is None:
        click.echo(
            f"  {style('!', fg='yellow')} {style(CLIENT_EXE_NAME, bold=True)}"
            f" {style('is not on PATH -- panes will not auto-reconnect.', fg='yellow')}"
        )
        click.echo(
            f"  {style('Reinstall with', dim=True)}"
            f" {style('pip install -U magent-multi-ai-agents-manager', bold=True)}"
            f"{style('.', dim=True)}"
        )
    titles: list[str] = []
    for sid in sids:
        title = make_title(sid)
        if sid in open_already:
            # Still tiled with everything else -- an already-open window belongs
            # in the grid; it just must not be opened a second time, and costs
            # no stagger since no SSH handshake follows.
            _echo_already_open(title)
            titles.append(title)
            continue
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
                *_pane_command(target, sid, supervisor),
            ]
        )
        titles.append(title)
        time.sleep(stagger)
    return titles


# Stagger for the RETRY batch. A casualty proves the host's sshd was already
# turning connections away at 4/sec, so the retry buys headroom with time: one
# handshake per second keeps roughly one connection unauthenticated at a time
# even when a loaded host takes several seconds to answer. The batch is only
# the windows that actually died, so the extra seconds are cheap.
_RETRY_STAGGER_S = 1.0

# Bounded settle before the FINAL scan. A respawned ssh that is still mid
# handshake is a live process carrying the attach marker, so it would pass a
# scan it is about to fail; the retry's own tiling pass usually covers that on
# its own, and this tops it up for the small-batch case where tiling returns
# almost immediately. Deliberately short and fixed -- it is only ever paid on
# the (rare) retry path, and a straggler is reported, never closed.
_RETRY_SETTLE_S = 5.0


def _verify_and_respawn(
    target: str, sids: Sequence[str], *, reconnect: bool = True
) -> None:
    """Reopen the windows whose supervisor never got a connection up at all.

    A big attach opens one SSH connection per window, and Windows OpenSSH has
    no ControlMaster, so they cannot be shared. During a bring-up storm the
    host is cold-starting dozens of agent processes and handshakes stretch from
    ~1s to many seconds; concurrent *unauthenticated* connections then pile up
    past sshd's MaxStartups (default 10:30:100) and it starts dropping
    newcomers probabilistically -- `Connection closed by <host> port 22` or
    `kex_exchange_identification: read: Connection reset`, then
    `[process exited with code 255]` in the pane.

    ``_repair_corpses`` already heals those, but only at the START of the next
    attach: the run that spawned them still ended with dead panes on screen.
    This closes that gap by checking the windows this run just opened.

    What the reconnect supervisor changed, and what it did NOT. A handshake
    casualty is now healed IN the pane: ssh exits 255, the supervisor waits two
    seconds and dials again, and the storm drains itself. So this pass fires
    far less often than it used to -- but it is not redundant, because it
    answers a different question. It asks whether anything at all is driving
    the pane, and the answer is still "no" when the supervisor could not be
    spawned (missing binary, blocked executable, wt refusing the window), when
    it stopped because the host answered with a failing remote command, or when
    the run used ``--no-reconnect``. Those panes are as dead as they ever were.

    Timing is still the trick, and now a friendlier one: a supervisor is a live
    process carrying the attach marker from the instant it starts -- through
    the handshake, through every backoff -- so this scan can only ever conclude
    "dead" about a pane that has genuinely stopped. Scanning right after the
    spawn loop would still be wrong (a supervisor that is about to fail to
    exec has not failed yet), so the caller keeps running this AFTER tiling and
    the geometry nudge.

    Costs the common (zero-casualty) case exactly one process scan and no
    output, and is skipped outright on platforms without the scan/close
    capabilities.
    """
    casualties = _repair_corpses(set(sids))
    if not casualties:
        return
    # Respawn in the caller's order so the retiling lands them predictably.
    retry = [sid for sid in sids if sid in casualties]
    click.echo(
        f"\n  {style('~', fg='yellow')} {style(str(len(retry)), bold=True)}"
        f" window(s) died during SSH handshake -- reopening"
        f" {style('(slower, to stay under the host connection limit)', dim=True)}"
    )
    get_logger("launch").info(
        "attach: respawning %d handshake casualt(y/ies): %s",
        len(retry),
        ", ".join(retry),
    )
    titles = _spawn_windows(target, retry, set(), _RETRY_STAGGER_S, reconnect=reconnect)
    _tile_titles(titles)
    _reclaim_geometry(titles)

    time.sleep(_RETRY_SETTLE_S)
    # Exactly one more look, and a read-only one: two spawns is the budget, so
    # anything still dead is reported and left on screen (its pane carries the
    # ssh error) instead of being closed for a third attempt that would never
    # come.
    still_dead = _dead_sids(set(retry))
    if not still_dead:
        return
    names = ", ".join(sorted(still_dead))
    get_logger("launch").warning(
        "attach: %d window(s) still dead: %s", len(still_dead), names
    )
    click.echo(
        f"\n  {style('!', fg='yellow')} {style(str(len(still_dead)), fg='yellow', bold=True)}"
        f" {style(f'window(s) still could not connect to {target}:', fg='yellow')}"
        f" {style(names, fg='yellow', bold=True)}"
    )
    click.echo(
        f"  {style('The host is likely still rate-limiting new SSH connections.', fg='yellow')}"
        f" {style('Re-run', fg='yellow')} {style('magent attach', fg='yellow', bold=True)}"
        f" {style('once it settles.', fg='yellow')}"
    )


def _close_attach_windows(names: Sequence[str]) -> int:
    """Close the local attach windows for ``names`` -- every magent: window
    when ``names`` is empty -- and report how many the OS accepted.

    Killing a psmux server makes every client attached to it exit, and a wt
    pane whose process exited keeps its magent: title: one corpse window per
    session. Closing them BEFORE the sessions die is what stops `down --host`
    from recreating, at scale, the very bug ``_repair_corpses`` repairs.

    An empty ``names`` deliberately means "all of them": a group-scoped or
    whole-machine shutdown cannot be resolved to a session subset from the
    client (group membership lives in the host's config), and closing a window
    is non-destructive -- the session it was viewing is what `down` is about
    to stop anyway.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    plat = get_platform()
    if not plat.supports_window_close():
        return 0
    snap = plat.snapshot_windows()
    return len(_close_windows(plat, snap, list(names) or _open_attach_sids(snap)))


# `magent down` on the host is a psmux kill fan-out plus a verify pass plus a
# couple of pid stops, after an SSH handshake, on a possibly-loaded box. The
# old 60s budget was measured against a handful of sessions and became a
# TRUNCATION mechanism at scale: 46 sockets could outrun it, ssh was killed
# mid-shutdown, and what survived was exactly the part of the config the
# shutdown had not reached yet -- the reported "the tail always stays" tail.
# Sized like `_BRING_UP_TIMEOUT_S`: generous enough that only a genuinely
# unreachable host hits it.
_REMOTE_DOWN_TIMEOUT_S = 300


def _remote_down_command(
    names: Sequence[str], group: str | None, do_all: bool, stop_srv: bool
) -> str:
    """The host-side `magent down` line for this local selection, verbatim.

    Quoting matches the rest of this module's remote commands (`-g "<group>"`).
    """
    parts = ["magent", "down", *(f'"{n}"' for n in names)]
    if group:
        parts += ["-g", f'"{group}"']
    if do_all:
        parts.append("--all")
    if stop_srv:
        parts.append("--server")
    return " ".join(parts)


def _remote_down(
    target: str,
    names: Sequence[str],
    group: str | None,
    do_all: bool,
    stop_srv: bool,
) -> int:
    """Run the user's `down` selection on the attach host, over SSH.

    Returns the exit code the calling shell should carry -- 0 on success, ssh's
    own otherwise. Never a silent no-op: on a laptop `magent down --all` used
    to stop the local Alt+V listener and nothing else, while attach's own
    goodbye line told the user that command stops their sessions.

    Local windows are closed FIRST (see ``_close_attach_windows``), because the
    remote kill is what strands them.
    """
    closed = _close_attach_windows(names)
    if closed:
        click.echo(
            f"  {style('~', fg='yellow')} Closed {style(str(closed), bold=True)}"
            f" local attach window(s)."
        )
    remote_cmd = _remote_down_command(names, group, do_all, stop_srv)
    click.echo(
        f"  {style('#', fg='cyan')} Stopping sessions on {style(target, bold=True)}"
        f" {style(f'({remote_cmd})', dim=True)}..."
    )
    rc, out, err = _ssh_capture(target, remote_cmd, timeout=_REMOTE_DOWN_TIMEOUT_S)
    if out.strip():
        click.echo(out.rstrip())
    if rc == 0:
        return 0
    click.echo(
        f"  {style('x', fg='red')} {target} did not run the shutdown"
        f" {style(f'(ssh exit {rc})', dim=True)}."
    )
    detail = err.strip().splitlines()[-1] if err.strip() else ""
    if detail:
        click.echo(f"  {style(detail[:200], dim=True)}")
    return rc


# Bringing up many agents at once is a cold-start storm on the host (each
# session spawns a shell plus a full agent process): a 40-project bring-up can
# genuinely run past five minutes on a loaded machine. The old 300s budget
# gave up mid-storm and re-queried once, capturing a partial session list --
# windows then opened onto sessions that weren't up yet.
_BRING_UP_TIMEOUT_S = 900
_STABILIZE_POLLS = 20
_STABILIZE_INTERVAL_S = 3


def _bring_up_and_requery(
    target: str,
    grp_suffix: str,
    fallback_up: list[dict[str, object]],
    expected: int,
) -> list[dict[str, object]]:
    click.echo(
        f"  {style('o', fg='cyan')} starting sessions on host "
        f"{style('(a large bring-up can take several minutes)', dim=True)}..."
    )
    rc, _, err = _ssh_capture(
        target, f"magent up{grp_suffix}", timeout=_BRING_UP_TIMEOUT_S
    )
    if rc != 0:
        click.echo(
            f"  {style('!', fg='yellow')} bring-up exited {rc}: {style(err.strip()[:200], dim=True)}"
        )
    # Sessions can still be materializing on a busy host even after `up`
    # returns (or times out) -- poll until every expected session is accounted
    # for, falling back to "the up-count stopped growing" when some of them
    # never come up, rather than trusting a single snapshot.
    best = fallback_up
    prev = -1
    for _ in range(_STABILIZE_POLLS):
        new = _ssh_json(target, f"magent up --json{grp_suffix}", timeout=60)
        if new:
            raw_up = new.get("up")
            cur = _as_session_list(raw_up) if isinstance(raw_up, list) else []
            if len(cur) >= len(best):
                best = cur
            # Everything asked for is up: the bring-up is complete, so there is
            # nothing left for stall detection to detect. Breaking here (before
            # the sleep) saves the second query plus a full interval -- ~10s on
            # the common "it all came up" path.
            if len(cur) >= expected:
                break
            # Partial bring-up: some sessions may never appear, so fall back to
            # waiting for the count to stop growing.
            if len(cur) == prev:
                break
            prev = len(cur)
        time.sleep(_STABILIZE_INTERVAL_S)
    return best


def _attach_flow(
    host: str | None,
    no_mux: bool = False,
    group: str | None = None,
    yes: bool = False,
    reconnect: bool = True,
) -> None:
    """Remote-PC attach: bring the host's sessions up, then open local windows.

    Default (psmux): tile one local window per remote psmux session and run the
    Alt+V image hotkey. ``--no-mux``: open one plain SSH window per project that
    runs the agent directly (no multiplexer). ``group`` limits the whole flow to
    one project group on the host; ``yes`` skips the bring-up prompt;
    ``reconnect`` (off via ``--no-reconnect``) decides whether each pane runs
    the reconnect supervisor or a bare ssh.
    """

    grp = f' -g "{group}"' if group else ""

    if not host:
        # Prefer the last successfully attached target over the config guess:
        # re-attaching to the same machine is the overwhelmingly common case.
        default = _read_last_host() or _default_attach_host()
        hint = "(user@host -- Enter reuses)" if default else "(user@host)"
        host = click.prompt(
            f"  {style('SSH host', fg='cyan')} {style(hint, dim=True)}",
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
    status, rc, err = _query_status(target, grp)
    if status is None:
        _explain_status_failure(target, rc, err)
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

    # The host answered with a real magent status -- worth remembering.
    _remember_last_host(target)
    # ...and worth checking against ours: a host on an older magent silently
    # lacks features this client assumes exist.
    _warn_version_skew(target, status)

    if no_mux:
        _attach_nomux(target, status)
        return

    raw_up = status.get("up")
    raw_down = status.get("down")
    up = _as_session_list(raw_up) if isinstance(raw_up, list) else []
    down = _as_session_list(raw_down) if isinstance(raw_down, list) else []
    port = status.get("upload_port", 8033)

    if down and yes:
        up = _bring_up_and_requery(target, grp, up, len(up) + len(down))
    elif down:
        pickable = _print_session_overview(hostname, up, down)
        # Host truth alone reads "ready" for a session whose local pane is a
        # corpse; say so BEFORE the prompt, while the user is still choosing.
        _annotate_dead_windows(up)
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
            up = _bring_up_and_requery(target, grp, up, len(up) + len(down))
        else:
            sel = None
            if choice.isdigit() and 1 <= int(choice) <= len(pickable):
                sel = pickable[int(choice) - 1]
            else:
                sel = next((g for g in pickable if g.lower() == choice), None)
            if sel:
                in_group = [
                    d
                    for d in down
                    if isinstance(g := d.get("group"), str) and g.lower() == sel.lower()
                ]
                up = _bring_up_and_requery(
                    target, f' -g "{sel}"', up, len(up) + len(in_group)
                )
            else:
                click.echo(
                    f"  {style('?', fg='yellow')} unrecognized choice -- bringing up none."
                )

    if not up:
        click.echo(f"\n  {style('x', fg='red')} No sessions are up on the host.")
        sys.exit(1)

    # The psmux socket id (P3-01): drives the window title, the wire the
    # Alt+V hotkey posts back, and the host-side `magent sessions <id>`.
    sids = [_as_str(s.get("session")) or _as_str(s.get("name")) for s in up]
    # An open window is not proof of a live attach: a dead SSH client leaves the
    # pane (and its title) behind, and title-only dedupe then skipped that
    # session forever. Sweep every magent: window here, not just the up ones --
    # a corpse whose session ALSO died is the one case no reopen can heal, and
    # the one the previous machinery could not even see. Corpses of live
    # sessions come back out of the spawn loop below; the rest just go away.
    open_already = _sweep_dead_windows(sids)

    titles = _spawn_windows(
        target, sids, open_already, _SPAWN_STAGGER_S, reconnect=reconnect
    )

    # Guarantee the host runs an upload server for Alt+V -- independent of the
    # host's uploadServer flag and of whether anything was just brought up.
    # Started before tiling so the SSH round-trip rides under the tiling poll
    # instead of adding its own 2-3s afterwards.
    ensure = subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            target,
            f"magent serve -p {port} --ensure",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _tile_titles(titles)
    # After placement, so the rect each window is restored to is the tiled one.
    _reclaim_geometry(titles)
    # ...and only now, once those two phases have given every handshake time to
    # either connect or fail, is a process scan meaningful (see the docstring).
    _verify_and_respawn(target, sids, reconnect=reconnect)

    try:
        rc = ensure.wait(timeout=15)
    except subprocess.TimeoutExpired:
        ensure.kill()
        rc = 1
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
        # The attach target rides along so F2 opens the project over VS Code
        # Remote-SSH -- the folders in the payload live on the host, not here.
        pid = _maybe_start_hotkey(server_url, target)
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
    """Open one plain SSH window per project, running the agent directly (no psmux).

    Deliberately NOT supervised by the reconnect client. Without a multiplexer
    the agent is a child of the ssh session itself, so a dropped connection
    kills the agent on the host -- there is no surviving session to reattach
    to, and dialing back in would silently start a SECOND agent on a
    conversation the user thinks is still running. Reconnect is a psmux
    feature because psmux is what makes the far side outlive the connection.
    """

    projects = _project_dicts(status)
    if not projects:
        click.echo(f"  {style('x', fg='red')} No eligible projects in the host config.")
        sys.exit(1)

    click.echo(
        f"  {style(str(len(projects)), fg='green', bold=True)} project(s) "
        f"{style('-- direct SSH, no multiplexer', dim=True)}\n"
    )

    # Window title = psmux socket id so the Alt+V hotkey resolves it (P3-01).
    sids = [_as_str(p.get("session")) or _as_str(p.get("name")) for p in projects]
    open_already = _already_open(sids)

    titles: list[str] = []
    for sid, p in zip(sids, projects, strict=True):
        title = make_title(sid)
        if sid in open_already:
            # Tiled but not re-spawned, and no stagger: see _attach_flow.
            _echo_already_open(title)
            titles.append(title)
            continue
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
        time.sleep(_SPAWN_STAGGER_S)

    _tile_titles(titles)
    # Same reason as the psmux path: an already-open window tiled back onto its
    # own rect never emits a resize, so nothing downstream re-reads the size.
    _reclaim_geometry(titles)
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
@click.option(
    "--revive",
    is_flag=True,
    help="Re-launch the agent in live sessions whose pane fell back to a bare shell",
)
@click.pass_context
def up_cmd(
    ctx: click.Context, as_json: bool, do_all: bool, group: str | None, revive: bool
) -> None:
    """Ensure a persistent psmux session per project (host side of `attach`)."""
    config_file = find_config(ctx.obj.get("config_path"))
    # Shared as_json config-error envelope (NF-S3-005): --json always gets JSON,
    # never a stderr Error: line. Folds the former up_cmd raw-loader exception.
    cfg = _load_config_or_exit(config_file, as_json=as_json)

    from magent.launch import (  # heavy subsystem: in-body per policy
        bring_up_psmux,
        decorate_psmux_sessions,
        decorate_psmux_sessions_async,
        psmux_status,
        revive_psmux,
    )

    up, down, projects = psmux_status(cfg, group=group)
    # Only sessions that were ALREADY up are revive candidates: one created
    # moments ago by bring_up_psmux may still be booting its agent, and typing
    # into that pane would double-launch.
    live_ids = [_as_str(d.get("session")) or _as_str(d.get("name")) for d in up]

    if as_json:
        # `up --json` is a pure read by default (attach polls it repeatedly);
        # reviving is opt-in so a poll never types into anyone's pane.
        revived = revive_psmux(cfg, only=live_ids, group=group) if revive else []
        # Refresh the F1/F2 status-line hints here too. `magent attach` drives
        # the host through THIS path (`magent up --json --revive` over SSH), so
        # skipping it left every pre-existing session hint-less on exactly the
        # flow the hints were built for. Idempotent and silent -- it writes to
        # the psmux status bar, never to stdout, so `--json` output stays pure
        # JSON. Sessions created by bring-up are decorated at birth by
        # launch_psmux_session, and `--json` never brings anything up, so
        # live_ids is the whole gap.
        #
        # No code_hint: this command runs ON the session host (attach drives it
        # over SSH), the status line is that host's, and psmux scopes it per
        # session rather than per client -- so the host's own `code` is the only
        # answer the protocol lets us give. Probed once inside, for all names.
        #
        # ...and it is the ASYNC variant here, which is load-bearing. The
        # synchronous one runs each session's commands under a 3s-timeout
        # subprocess.run, so a host busy enough for those to time out spent ~15s
        # per session decorating before this command printed a byte of JSON --
        # past the attach client's 30s status timeout, which then retried with a
        # 120s one and re-ran the whole thing. A status query must never wait on
        # a cosmetic status bar; the async variant fires and returns, throttled
        # by a stamp so attach's repeated polls can't pile up processes.
        decorate_psmux_sessions_async(live_ids)
        # deferred: resolving __version__ costs an importlib.metadata import,
        # and only the JSON envelope needs it (see cli/ui.py::_banner).
        from magent import __version__

        click.echo(
            json.dumps(
                {
                    # P3-03: snake_case across all CLI JSON; P3-04: ok-envelope.
                    "ok": True,
                    # The attach client compares this against its own version
                    # and warns on skew; a host too old to emit it is exactly
                    # the case that warning exists for.
                    "version": __version__,
                    "platform": sys.platform,
                    "psmux": cfg.settings.psmux,
                    "upload_server": cfg.settings.upload_server,
                    "upload_port": cfg.settings.upload_port,
                    # up/down entries already carry name (display) + session
                    # (psmux socket id) from psmux_status (P3-01).
                    "up": up,
                    "down": down,
                    # Always present (empty without --revive) so a consumer can
                    # read it unconditionally. P3-03: snake_case.
                    "revived": revived,
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
    created: list[str] = []
    if not projects:
        where = f" in group '{group}'" if group else ""
        click.echo(f"  {style('!', fg='yellow')} No eligible projects{where}.")
    elif not do_all and not down:
        click.echo(f"  {style('+', fg='green')} All {len(up)} session(s) already up.")
    else:
        created, failed = bring_up_psmux(cfg, only=targets, group=group)
        click.echo(
            f"  {style('+', fg='green')} Brought up {style(str(len(created)), fg='green', bold=True)}"
            f" session(s): {style(', '.join(created) or '(none)', dim=True)}"
        )
        # `created` now means "the verify proved it is up", so the sessions it
        # does NOT contain have to be named -- this line is what `magent attach`
        # relays from the host, and a silent casualty there reads as success.
        if failed:
            click.echo(
                f"  {style('x', fg='red')} {style(str(len(failed)), fg='red', bold=True)}"
                f" session(s) failed to come up: {style(', '.join(failed), fg='red')}"
                f" {style('(see ~/.magent/logs/launch.log on the host)', dim=True)}"
            )

    # Unconditional on the interactive path: a session that is up but parked at
    # a bare shell is exactly what this command is asked to fix, and there is
    # no poll here to keep pure (unlike --json).
    revived = revive_psmux(cfg, only=live_ids, group=group)
    if revived:
        click.echo(
            f"  {style('+', fg='green')} Revived agent in"
            f" {style(str(len(revived)), fg='green', bold=True)}"
            f" session(s): {style(', '.join(revived), dim=True)}"
        )

    # Advertise the F1/F2 hints in every live session's status line. Sessions
    # created by launch_psmux_session are decorated at birth; doing it again
    # here is what gives a PRE-EXISTING session (made before this feature, or
    # by an older magent) the hints without forcing a recreate. The `--json`
    # branch above does the same for its live sessions. Same host-side `code`
    # probe as there: one for the batch, not one per session.
    decorate_psmux_sessions([*live_ids, *created])

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
@click.option(
    "--no-reconnect",
    is_flag=True,
    help="Don't auto-reconnect attach panes when the SSH connection drops",
)
@click.pass_context
def attach_cmd(
    ctx: click.Context,
    host: str | None,
    no_mux: bool,
    group: str | None,
    yes: bool,
    no_reconnect: bool,
) -> None:
    """Attach to another machine's magent sessions over SSH.

    HOST is user@host (omit to be prompted; the prompt defaults to the last
    host you attached to, falling back to your local config). Default tiles one
    window per remote psmux session with Alt+V image paste; --no-mux opens a
    direct SSH window per project instead. -g limits the flow to one project
    group on the host; -y skips the bring-up prompt.

    Each pane survives a dropped connection and reattaches on its own once the
    host is reachable again; --no-reconnect restores the old one-shot ssh pane.
    """
    _attach_flow(host, no_mux=no_mux, group=group, yes=yes, reconnect=not no_reconnect)


@main.command("hotkey")
@click.option(
    "--server", "-s", default="http://localhost:8033", help="Upload server URL"
)
@click.option(
    "--ssh-host",
    default=None,
    help="SSH target whose projects F2 opens over VS Code Remote-SSH",
)
@click.pass_context
def hotkey_cmd(ctx: click.Context, server: str, ssh_host: str | None) -> None:
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
    click.echo(
        f"  {style('F2', fg='cyan', bold=True)} opens the focused project's folder in VS Code"
        + (f" {style(f'(over SSH: {ssh_host})', dim=True)}" if ssh_host else "")
    )
    click.echo(f"  {style('Only active in windows titled magent:<project>', dim=True)}")
    click.echo(f"  {style('Ctrl+C to stop.', dim=True)}")
    click.echo()

    from magent.hotkey import (
        run_hotkey,  # ImportError off-Windows (hotkey.py guards); must stay lazy
    )

    try:
        run_hotkey(server, ssh_host)
    except KeyboardInterrupt:
        click.echo(f"\n  {style('Stopped.', dim=True)}")
    except RuntimeError as e:
        click.echo(f"  {style('x', fg='red')} {e}")
        sys.exit(1)
