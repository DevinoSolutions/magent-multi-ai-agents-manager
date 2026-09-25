"""The node sync daemon: mirror every node's sessions, load and agent state onto this PC.

Layout under ``nodes.NODES_DIR`` (``~/.magent/nodes``; the path helpers in
``nodes`` are its one owner)::

    <nick>/sessions.json          liveness: {"ts": <PC epoch>, "sessions": [...]}
    <nick>/load.jsonl             one LoadSample per line (ts on this PC's clock)
    <nick>/pull.json              the watermark: {sid: {"since", "realpath"}}
    <nick>/<sid>/transcripts/     the node's ~/.claude/projects/<dir>/ contents
    <nick>/<sid>/state/           that session's agent-state records

Each tick makes ONE ssh per node (``remote_mux.pull_node`` runs ``pull.sh``
once, and that connection answers liveness, load AND files). Nodes are pulled
in parallel and fail alone. An unreachable node keeps its last snapshot: its
``sessions.json`` ts stops advancing, which is how readers call it stale. It is
logged once when it goes down and once when it comes back, never every tick.

The daemon shape is ``magent attention -d``'s: ``lockfile.exclusive_lock``, a pid
file, a heartbeat thread, and a heartbeat left behind as the crash marker.
``magent serve`` keeps it alive (``launch.ensure_node_sync``).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from magent import nodes
from magent.config import NODE_CLOUD, load_config
from magent.lockfile import LockHeld, exclusive_lock
from magent.log import clear_heartbeat, get_logger
from magent.procs import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from magent.config import MagentConfig

# The daemon's ONE name, and the only "node-sync" literal in src/ (a source
# test pins that). The heartbeat (log.run_heartbeat / log.heartbeat_age), the
# daemon lock, the pid file, the tick's thread names and serve's supervisor
# lock all derive from it. Every reader imports it from here --
# node_cmd._daemon_state, launch.ensure_node_sync.
HEARTBEAT_NAME = "node-sync"
LOCK_NAME = HEARTBEAT_NAME
SUPERVISOR_LOCK_NAME = f"{HEARTBEAT_NAME}-supervisor"
LOG_NAME = "nodes"
# Held around every pull of one node, so `down`'s final pull and the daemon's
# tick never write the same mirror at once.
NODE_LOCK_PREFIX = "node-pull-"
_PID_PATH = Path.home() / ".magent" / f"{HEARTBEAT_NAME}.pid"

# One tick's outcome per node.
OK = "ok"
UNREACHABLE = "unreachable"  # ssh transport failure (255) or no answer in time
FAILED = "failed"  # the node answered, and the answer was not a pull
MISCONFIGURED = "misconfigured"  # the nick does not resolve (D4, no user)
LOCKED = "locked"  # another pull holds this node right now


def daemon_pid() -> int | None:
    """PID of the running node sync daemon, or None. Clears a stale pid file."""
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
    """Stop the node sync daemon. True only if a kill was issued and the
    process is confirmed gone. A forced kill skips the daemon's own cleanup, so
    this owns the heartbeat removal that tells 'off' from 'crashed'."""
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
    return False


def wanted(config: MagentConfig) -> bool:
    """Is there anything to sync: a pool, and a project pinned or placed on it?
    A ``"cloud"`` project has no pool node and is not the daemon's."""
    return bool(config.settings.nodes) and any(
        p.node is not None and p.node != NODE_CLOUD for p in config.projects
    )


def tick_interval_s(config: MagentConfig) -> float:
    """Seconds between ticks: the shorter of the pull and sample intervals,
    because one ssh carries both."""
    sync = config.settings.node_sync
    return float(max(1, min(sync.pull_interval_s, sync.sample_interval_s)))


def state_stores() -> list[tuple[str, str, Path]]:
    """``(project, "@<nick>", mirrored state dir)`` for every placed node
    session -- ``attention.AttentionEngine(extra_stores=...)``'s input. The
    project (the node map's key) is the name its window carries; the
    ``@<nick>`` key keeps two nodes' identical directories apart."""
    # TODO(E7): skip sids that fail remote_mux.pullable_sid
    return [
        (project, f"@{e.nick}", nodes.state_dir(e.nick, e.sid))
        for project, e in sorted(nodes.read_node_map().items())
    ]


@contextlib.contextmanager
def node_lock(
    nick: str,
    *,
    wait_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Hold ``node-pull-<nick>`` for one pull, retrying every 0.2s for up to
    ``wait_s``. LockHeld when it stays taken. Only ACQUIRING is retried: a
    LockHeld raised inside the body propagates as itself."""
    deadline = now() + wait_s
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(exclusive_lock(NODE_LOCK_PREFIX + nick))
                break
            except LockHeld:
                if now() >= deadline:
                    raise
                sleep(0.2)
        yield


class ConfigWatch:
    """The config file, reloaded only when its mtime changes, keeping the last
    good one through a broken edit. A long-running reader (the daemon, serve's
    supervisor) must pick up a new pool without printing the config's load
    warnings on every tick."""

    def __init__(self, path: Path, config: MagentConfig | None = None) -> None:
        self._path = path
        self._config = config
        self._mtime = self._stat() if config is not None else None

    def _stat(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def current(self) -> MagentConfig | None:
        mtime = self._stat()
        if mtime is None:
            get_logger(LOG_NAME).debug("node sync: no config at %s", self._path)
            return self._config
        if mtime == self._mtime:
            return self._config
        self._mtime = mtime
        try:
            self._config = load_config(str(self._path))
        except (ValueError, OSError) as e:
            get_logger(LOG_NAME).warning(
                "node sync: config %s did not load (%s); keeping the last good one",
                self._path,
                e,
            )
        return self._config
