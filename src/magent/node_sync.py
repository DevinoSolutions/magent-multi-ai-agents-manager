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
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from magent import nodes, remote_mux
from magent.attach_client import SSH_TRANSPORT_RC
from magent.config import NODE_CLOUD, load_config
from magent.env import local_username
from magent.lockfile import LockHeld, exclusive_lock
from magent.log import clear_heartbeat, get_logger
from magent.procs import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from magent.config import MagentConfig
    from magent.nodes import Node, NodeMapEntry

# The daemon's ONE name, and the only "node-sync" literal in src/ (DECISION-17,
# DECISION-26 i; a source test pins that). The heartbeat (log.run_heartbeat /
# log.heartbeat_age), the daemon lock, the pid file, the tick's thread names and
# serve's supervisor lock all derive from it. Every reader imports it from here
# -- node_cmd._daemon_state, launch.ensure_node_sync.
HEARTBEAT_NAME = "node-sync"
LOCK_NAME = HEARTBEAT_NAME
SUPERVISOR_LOCK_NAME = f"{HEARTBEAT_NAME}-supervisor"
LOG_NAME = "nodes"
# Held around every pull of one node, so `down`'s final pull and the daemon's
# tick never write the same mirror at once.
NODE_LOCK_PREFIX = "node-pull-"
# How often node_lock retries a held node lock while it waits.
NODE_LOCK_RETRY_S = 0.2
_PID_PATH = Path.home() / ".magent" / f"{HEARTBEAT_NAME}.pid"

# One tick's outcome per node.
OK = "ok"
UNREACHABLE = "unreachable"  # ssh transport failure (255) or no answer in time
# Everything else: a bad answer, a local error (an ssh that will not start, an
# over-cap reply, a disk write), or a bug ("internal error: <type>").
FAILED = "failed"
MISCONFIGURED = "misconfigured"  # the nick does not resolve (D4, no user)
LOCKED = "locked"  # another pull holds this node right now
# Not an outcome (tick reports it as FAILED): the state _note keeps for a node
# whose pull raised something unexpected, so its ERROR is logged once.
_INTERNAL_ERROR = "internal error"


def daemon_running() -> bool:
    """Is a node sync daemon alive right now?

    Answered by the daemon's lock, never by the pid file. The daemon holds
    ``LOCK_NAME`` for its whole life and the OS releases it the instant the
    process dies, so the lock cannot outlive its holder. A pid file can: after a
    crash or a reboot it survives, and Windows recycles the number onto an
    unrelated process, which ``pid_alive`` then calls the daemon. The probe
    takes the lock and lets it go at once; only a holder removes the lock file,
    so a probe never disturbs a running daemon's lock.
    """
    try:
        with exclusive_lock(LOCK_NAME):
            return False
    except LockHeld:
        return True


def daemon_pid() -> int | None:
    """The pid the pid file names, if that process is alive, else None. Clears
    a pid file whose process is gone.

    A live pid is NOT proof of a daemon -- ask ``daemon_running``. Once the
    lock has said a daemon exists, this is its pid: the kill target and the log
    line."""
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


def _kill(pid: int) -> bool:
    """Ask ``pid`` to die. True when the request was accepted."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False
        )
        return result.returncode == 0
    try:
        os.kill(pid, 15)  # SIGTERM
    except OSError:
        return False
    return True


def _clear_leftovers() -> None:
    """Remove the pid file and the heartbeat, so ``status`` reads 'off'."""
    with contextlib.suppress(OSError):
        _PID_PATH.unlink()
    clear_heartbeat(HEARTBEAT_NAME)


# How long stop_daemon waits for a killed daemon to be gone, and how often it
# looks. SIGTERM is asynchronous on POSIX: the pid still answers for a moment.
STOP_SETTLE_S = 2.0
STOP_POLL_S = 0.1


def stop_daemon(
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Stop the node sync daemon. True when no daemon is left running and
    there was something to stop or clear; False when there was nothing at all,
    or when the kill did not land within ``STOP_SETTLE_S``.

    Only a daemon that holds the lock is killed (``daemon_running``). With the
    lock free there is no daemon, whatever the pid file says -- its number may
    belong to a stranger by now -- so the leftovers are cleared and nothing is
    killed. A forced kill skips the daemon's own cleanup, so this owns the
    heartbeat removal that tells 'off' from 'crashed'."""
    if not daemon_running():
        had_pid = _PID_PATH.exists()
        _clear_leftovers()
        return had_pid
    pid = daemon_pid()
    if not pid or not _kill(pid):
        return False
    deadline = now() + STOP_SETTLE_S
    while pid_alive(pid):
        if now() >= deadline:
            return False
        sleep(STOP_POLL_S)
    _clear_leftovers()
    return True


def wanted(config: MagentConfig) -> bool:
    """Is there anything to sync: a pool, and an enabled project pinned or
    placed on it? A ``"cloud"`` project has no pool node and is not the
    daemon's.

    This reads the config only and ignores the node map, so removing (or
    disabling) the last node project stops the sync even for sessions still
    live on a node. Deliberate (YAGNI); revisit if it bites."""
    return bool(config.settings.nodes) and any(
        p.enabled and p.node is not None and p.node != NODE_CLOUD
        for p in config.projects
    )


def tick_interval_s(config: MagentConfig) -> float:
    """Seconds between ticks: the shorter of the pull and sample intervals,
    because one ssh carries both."""
    sync = config.settings.node_sync
    return float(max(1, min(sync.pull_interval_s, sync.sample_interval_s)))


# (project, nick, sid) entries state_stores has already warned about.
_UNPULLABLE_WARNED: set[tuple[str, str, str]] = set()


def state_stores() -> list[tuple[str, str, Path]]:
    """``(project, "@<nick>", mirrored state dir)`` for every placed node
    session -- ``attention.AttentionEngine(extra_stores=...)``'s input. The
    project (the node map's key) is the name its window carries; the
    ``@<nick>`` key keeps two nodes' identical directories apart.

    Read STRICTLY (``nodes.load_node_map_strict``): a missing map is no
    placements and yields ``[]``, but a torn map or one still busy after its
    retries RAISES (``ValueError`` / ``OSError``). A tolerant ``{}`` here would
    be a successful listing with zero roots, and the attention engine would
    drop every node row for that tick; the error lets it hold each root's last
    records and warn instead.

    An entry whose sid fails ``remote_mux.pullable_sid`` is skipped:
    ``state_dir`` joins a sid verbatim, so ``/etc`` would name a store outside
    the nodes dir. The daemon's tick refuses the same sids, so no mirror
    exists for one anyway. Warned once per entry per process -- this runs on
    every attention tick."""
    stores: list[tuple[str, str, Path]] = []
    for project, e in sorted(nodes.load_node_map_strict().items()):
        if not remote_mux.pullable_sid(e.sid):
            if (project, e.nick, e.sid) not in _UNPULLABLE_WARNED:
                _UNPULLABLE_WARNED.add((project, e.nick, e.sid))
                get_logger(LOG_NAME).warning(
                    (
                        "node sync: not reading %s's state: its session name %r "
                        "on %s is not a directory name on this PC"
                    ),
                    project,
                    e.sid,
                    e.nick,
                )
            continue
        stores.append((project, f"@{e.nick}", nodes.state_dir(e.nick, e.sid)))
    return stores


class NodeLockHeld(LockHeld):
    """``node_lock`` could not take ``node-pull-<nick>``: another pull of the
    same node holds it. Its own type so a caller can tell this contention from
    a LockHeld raised INSIDE the lock's body, which is some other lock."""


@contextlib.contextmanager
def node_lock(
    nick: str,
    *,
    wait_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """Hold ``node-pull-<nick>`` for one pull, retrying every
    ``NODE_LOCK_RETRY_S`` for up to ``wait_s``. NodeLockHeld when it stays
    taken. Only ACQUIRING is retried: a LockHeld raised inside the body
    propagates as itself, never as NodeLockHeld."""
    deadline = now() + wait_s
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(exclusive_lock(NODE_LOCK_PREFIX + nick))
                break
            except LockHeld as e:
                if now() >= deadline:
                    raise NodeLockHeld(*e.args) from e
                sleep(NODE_LOCK_RETRY_S)
        yield


def config_stamp(path: Path) -> tuple[int, int] | None:
    """``(st_mtime_ns, st_size)`` of ``path``, or None when it cannot be read.
    The size catches a rewrite that lands inside one mtime tick."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class ConfigWatch:
    """The config file, reloaded only when its stamp (``config_stamp``: mtime
    and size) changes, keeping the last good one through a broken edit. A
    long-running reader (the daemon, serve's supervisor) must pick up a new
    pool without printing the config's load warnings on every tick.

    A file that fails to load is tried ONCE per stamp: the last good config
    stays in force and the broken file is not retried until it changes again.

    A caller that already loaded the config passes it as ``config`` together
    with the ``stamp`` it read BEFORE that load, so an edit made between the
    load and this constructor is still picked up. Without ``stamp`` the file
    is stamped here, which misses such an edit. Without ``config`` the watch
    stats and loads the file itself on the first ``current()``."""

    def __init__(
        self,
        path: Path,
        config: MagentConfig | None = None,
        *,
        stamp: tuple[int, int] | None = None,
    ) -> None:
        self._path = path
        self._config = config
        self._stamp: tuple[int, int] | None = None
        if config is not None:
            self._stamp = stamp if stamp is not None else config_stamp(path)

    def current(self) -> MagentConfig | None:
        stamp = config_stamp(self._path)
        if stamp is None:
            get_logger(LOG_NAME).debug("node sync: no config at %s", self._path)
            return self._config
        if stamp == self._stamp:
            return self._config
        self._stamp = stamp
        try:
            self._config = load_config(str(self._path))
        except (ValueError, OSError) as e:
            get_logger(LOG_NAME).warning(
                "node sync: config %s did not load (%s); keeping the last good one",
                self._path,
                e,
            )
        return self._config


@dataclass(frozen=True)
class Mark:
    """One session's ``pull.json`` entry: the node-clock watermark and the real
    path it was taken for (a different path is a different transcript dir)."""

    since: float
    realpath: str | None


def _read_marks(nick: str) -> dict[str, Mark]:
    try:
        raw = json.loads(nodes.pull_marks_path(nick).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Mark] = {}
    for sid, value in raw.items():
        if not isinstance(sid, str) or not isinstance(value, dict):
            continue
        since, real = value.get("since"), value.get("realpath")
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            continue
        # json.loads accepts NaN/Infinity and arbitrarily long integers; neither
        # is a time, and float() of a 309+-digit int raises OverflowError. A bad
        # mark is no mark: that session is pulled from the beginning.
        try:
            since_f = float(since)
        except OverflowError:
            continue
        if not math.isfinite(since_f):
            continue
        out[sid] = Mark(since=since_f, realpath=real if isinstance(real, str) else None)
    return out


def _spec_for(entry: NodeMapEntry, mark: Mark | None) -> remote_mux.SidPull:
    real = mark.realpath if mark is not None else None
    return remote_mux.SidPull(
        roots=(entry.remote_root,),
        project_dir=nodes.encoded_project_dir(real) if real else None,
        since=mark.since if mark is not None else 0.0,
    )


# C0 and C1 control characters and DEL, minus tab: a node's stderr is its own
# words, and an ESC sequence in it must not reach a terminal tailing the log.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def _last_line(text: str) -> str:
    """The last non-blank line of a node's stderr, with every control
    character but tab shown as ``?``."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return _CONTROL.sub("?", lines[-1]) if lines else ""


def _classify(e: remote_mux.RemoteError) -> tuple[str, str]:
    """UNREACHABLE only when the node could not be reached (ssh's transport rc
    255) or never answered (``timed_out``). Every other rc-None error -- a
    reply over the cap, a local ssh that would not start -- is FAILED."""
    detail = _last_line(e.stderr_tail) or f"rc={e.rc}"
    if e.timed_out or e.rc == SSH_TRANSPORT_RC:
        return UNREACHABLE, detail
    return FAILED, detail


def _pull_node(
    node: Node, sids: Mapping[str, remote_mux.SidPull]
) -> remote_mux.NodeSnapshot:
    # PULL_TIMEOUT_S is read at call time so a test can shorten it.
    return remote_mux.pull_node(node, sids, timeout_s=remote_mux.PULL_TIMEOUT_S)


class NodeSyncer:
    """One tick = one pull per pool node, in parallel. ``pull`` is the seam
    (default: ``remote_mux.pull_node``); ``now`` is this PC's clock, which
    stamps ``sessions.json`` and ``load.jsonl``."""

    def __init__(
        self,
        config: MagentConfig,
        *,
        pull: Callable[
            [Node, Mapping[str, remote_mux.SidPull]], remote_mux.NodeSnapshot
        ]
        | None = None,
        now: Callable[[], float] = time.time,
        local_user: str | None = None,
        lock_wait_s: float = 0.0,
    ) -> None:
        self._config = config
        self._pull = pull if pull is not None else _pull_node
        self._now = now
        self._local_user = local_user
        self._lock_wait_s = lock_wait_s
        self._warned: set[tuple[str, str]] = set()
        # The last noted state per nick: an outcome, or _INTERNAL_ERROR.
        self._last: dict[str, str] = {}
        # A node's unexpected exception, from its worker thread to _note. One
        # key per nick, and one worker per nick at a time, so no two threads
        # write the same key.
        self._errors: dict[str, Exception] = {}

    def reconfigure(self, config: MagentConfig) -> None:
        self._config = config

    def tick(self) -> dict[str, tuple[str, str]]:
        """Pull every pool node once; ``{nick: (outcome, detail)}``. Every
        node is dialled -- one with no placed session still reports its
        liveness and load. Nothing a node answers raises out of here."""
        by_nick: dict[str, dict[str, NodeMapEntry]] = {}
        for entry in nodes.read_node_map().values():
            by_nick.setdefault(entry.nick, {})[entry.sid] = entry
        user = self._local_user if self._local_user is not None else local_username()
        pool = sorted(self._config.settings.nodes)
        if not pool:
            return {}
        with ThreadPoolExecutor(
            max_workers=min(8, len(pool)), thread_name_prefix=HEARTBEAT_NAME
        ) as ex:
            futures = {
                nick: ex.submit(self._sync_node, nick, by_nick.get(nick, {}), user)
                for nick in pool
            }
            results = {nick: f.result() for nick, f in futures.items()}
        for nick, (outcome, detail) in results.items():
            self._note(nick, outcome, detail)
        return results

    def _sync_node(
        self, nick: str, entries: Mapping[str, NodeMapEntry], local_user: str
    ) -> tuple[str, str]:
        """One node's pull, reduced to an outcome. Nothing raises out of here:
        what a node (or its config) can do has its own outcome, and anything
        else is a bug in this PC's code, which fails THIS node alone as
        ``internal error: <type>`` and hands the exception to ``_note``."""
        self._errors.pop(nick, None)
        try:
            node = nodes.node_for_nick(self._config, nick, local_user=local_user)
            with node_lock(nick, wait_s=self._lock_wait_s):
                self._pull_and_store(node, entries)
        except nodes.NodeConfigError as e:
            return MISCONFIGURED, str(e)
        except NodeLockHeld:
            return LOCKED, "another pull holds this node"
        except LockHeld as e:
            # Raised inside the node lock, so not the node's own lock: nothing
            # the pull does takes one, which makes it a bug, not contention.
            return self._internal_error(nick, e)
        except remote_mux.RemoteError as e:
            return _classify(e)
        except OSError as e:
            return FAILED, str(e)
        except Exception as e:  # noqa: BLE001  # reason: one node's bug must not stop the other nodes' pulls or crash-loop the daemon; _note logs it with its traceback once per state change
            return self._internal_error(nick, e)
        return OK, ""

    def _internal_error(self, nick: str, e: Exception) -> tuple[str, str]:
        self._errors[nick] = e
        return FAILED, f"internal error: {type(e).__name__}"

    def _note(self, nick: str, outcome: str, detail: str) -> None:
        """One log line per state CHANGE: a node down for a day is one warning
        and one "reachable again", not 2,880 lines. A locked tick is no state
        (the other pull is doing the work) and is never logged.

        An internal error is a state of its own, logged at ERROR with its
        traceback (so Sentry gets one event per change, never one per tick)."""
        if outcome == LOCKED:
            return
        error = self._errors.pop(nick, None)
        state = _INTERNAL_ERROR if error is not None else outcome
        prev = self._last.get(nick)
        self._last[nick] = state
        if state == prev:
            return
        log = get_logger(LOG_NAME)
        if error is not None:
            log.error("node %s: %s (%s)", nick, outcome, detail, exc_info=error)
            return
        if outcome == OK:
            if prev == UNREACHABLE:
                log.info("node %s: reachable again", nick)
            elif prev is not None:
                log.info("node %s: ok again (was %s)", nick, prev)
            return
        log.warning("node %s: %s (%s)", nick, outcome, detail)

    def _warn_once(self, nick: str, sid: str, why: str) -> None:
        if (nick, sid) in self._warned:
            return
        self._warned.add((nick, sid))
        get_logger(LOG_NAME).warning(
            "node %s: session %r %s; skipping it", nick, sid, why
        )

    def _pull_and_store(self, node: Node, entries: Mapping[str, NodeMapEntry]) -> None:
        marks = _read_marks(node.nick)
        specs: dict[str, remote_mux.SidPull] = {}
        for sid, entry in sorted(entries.items()):
            if not remote_mux.pullable_sid(sid):
                self._warn_once(node.nick, sid, "cannot be mirrored on this PC")
                continue
            if not entry.remote_root:
                self._warn_once(
                    node.nick, sid, "has an empty remote_root in the node map"
                )
                continue
            specs[sid] = _spec_for(entry, marks.get(sid))
        snap = self._pull(node, specs)
        self._store(node.nick, snap, at=self._now())

    def _store(self, nick: str, snap: remote_mux.NodeSnapshot, *, at: float) -> None:
        nodes.write_json_atomic(
            nodes.sessions_path(nick), {"ts": at, "sessions": list(snap.sessions)}
        )
