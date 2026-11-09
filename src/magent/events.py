"""The in-process event bus behind ``/api/v1/events`` and the poller feeding it.

``EventBus`` is a bounded ring of ``Event``s with monotonically numbered ids
``<epoch>:<n>``. ``epoch`` is 8 hex chars drawn when ``serve`` starts, so a
client resuming against a restarted server is told to refetch (``reset``
``epoch``) instead of silently missing what happened in between; a client
that fell more than ``ring`` events behind gets ``reset`` ``evicted``.

``EventPoller`` turns the fleet into events: every second it diffs the hook
half of ``fleetview.rows`` (the agent-state files plus one ``has-session``
probe per configured session: a fan-out under one ``PROBE_TIMEOUT_S``
deadline, its misses re-probed once under another, so the liveness part of
a tick is bounded at 2 x ``PROBE_TIMEOUT_S`` however many sockets are
wedged), every fifth second the pane half (one ``capture-pane`` per live
session), and every second it tails the panes a client asked for -- each
only while it is a configured session (the API refuses any other name, and
every tick checks again against its own rows before a capture). It
runs only while somebody listens (``EventBus.subscribers``) and only when
``MAGENT_EVENTS`` allows it -- the opt-out is a test-isolation law like the
other serve threads, pinned to 0 in ``tests/conftest.py``. A tick that
outruns ``SLOW_TICK_FACTOR`` intervals is warned about once per slow streak
(``run_poller``).

A leaf over ``fleetview``, ``control``, ``psmux`` and ``log``; never imports
the cli package (LS-A-001).
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import secrets
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from magent.attention import AttentionEngine
    from magent.config import MagentConfig
    from magent.fleetview import SessionRow
    from magent.psmux import PaneCapture

EventType = Literal[
    "hello",
    "session.added",
    "session.removed",
    "session.state",
    "session.pane",
    "attention",
    "upload",
    "project.changed",
    "reset",
]
ResetReason = Literal["epoch", "evicted"]

RING_SIZE = 1024
HOOK_INTERVAL_S = 1.0
PANE_EVERY_N_TICKS = 5
HEARTBEAT_S = 15.0
# The bound on each liveness probe the poller's tick makes: a wedged psmux
# server answers "not live" after this long instead of parking the poller
# (the status/down surfaces wait unbounded on purpose; a poller must not).
PROBE_TIMEOUT_S = 5.0
# A tick slower than this many intervals is reported (once per slow streak),
# and the streak is over only after this many consecutive ticks fit again --
# hysteresis, so a fleet hovering at the budget is not a log line a tick.
SLOW_TICK_FACTOR = 2.0
SLOW_TICK_CLEAR_TICKS = 3

# The row fields each half of the poller owns. A hook tick reads no pane, so
# it must never report the pane fields as changed (to None), and vice versa.
HOOK_FIELDS = ("live", "hook_state", "hook_state_stale", "node_state", "session_id")
PANE_FIELDS = ("pane_state", "model", "effort")

# Hook states that page: the relay pushes an `attention` event with fire=true.
ATTENTION_STATES = frozenset({"needs-input", "error", "done"})


@dataclass(frozen=True)
class Event:
    """One event as it travels: SSE ``data:``, long-poll ``events[]`` item."""

    id: str
    type: EventType
    ts: float
    data: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "type": self.type, "ts": self.ts, "data": self.data}


@dataclass(frozen=True)
class Since:
    """What a client resuming from ``last_id`` gets: the events after it, or
    a ``reset`` reason when the gap cannot be replayed; ``next`` is the id to
    resume from next time."""

    events: list[Event]
    next: str
    reset: ResetReason | None = None


def new_epoch() -> str:
    return secrets.token_hex(4)


def _parse_id(event_id: str) -> tuple[str, int] | None:
    epoch, sep, n = event_id.partition(":")
    if not sep or not n.isdigit():
        return None
    return epoch, int(n)


class EventBus:
    """A bounded, thread-safe event ring with long-poll waiting."""

    def __init__(
        self,
        epoch: str | None = None,
        *,
        ring: int = RING_SIZE,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.epoch = epoch or new_epoch()
        self._ring: deque[Event] = deque(maxlen=ring)
        self._n = 0
        self._now = now
        self._cond = threading.Condition()
        self._subscribers = 0
        self._pane_interest: Counter[str] = Counter()

    # -- publishing ----------------------------------------------------------

    def publish(self, type_: EventType, data: dict[str, object]) -> Event:
        """Append one event. ``data`` is copied: what the ring replays is what
        was published, whatever the caller does to its dict afterwards."""
        with self._cond:
            self._n += 1
            event = Event(
                id=f"{self.epoch}:{self._n}",
                type=type_,
                ts=self._now(),
                data=dict(data),
            )
            self._ring.append(event)
            self._cond.notify_all()
            return event

    def last_id(self) -> str:
        with self._cond:
            return f"{self.epoch}:{self._n}"

    # -- reading -------------------------------------------------------------

    def since(self, last_id: str | None) -> Since:
        """The events after ``last_id``. ``None`` means "from now": nothing to
        replay. A foreign epoch or an id older than the ring is a reset."""
        with self._cond:
            return self._since_locked(last_id)

    def _since_locked(self, last_id: str | None) -> Since:
        head = f"{self.epoch}:{self._n}"
        if last_id is None:
            return Since(events=[], next=head)
        parsed = _parse_id(last_id)
        if parsed is None or parsed[0] != self.epoch or parsed[1] > self._n:
            return Since(events=[], next=head, reset="epoch")
        after = parsed[1]
        oldest = self._n - len(self._ring) + 1
        if after < oldest - 1:
            return Since(events=[], next=head, reset="evicted")
        # Ids are consecutive, so the event numbered n sits at ring index
        # n - oldest: everything after `after` starts at after - oldest + 1.
        events = list(itertools.islice(self._ring, after - oldest + 1, None))
        return Since(events=events, next=head)

    def wait(self, last_id: str | None, timeout: float) -> Since:
        """Like ``since``, but block up to ``timeout`` for a first event.
        A reset answers at once."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                got = self._since_locked(last_id)
                if got.events or got.reset is not None:
                    return got
                left = deadline - time.monotonic()
                if left <= 0:
                    return got
                if last_id is None:
                    last_id = got.next
                self._cond.wait(left)

    # -- who is listening -----------------------------------------------------

    @contextlib.contextmanager
    def subscription(self, panes: tuple[str, ...] = ()) -> Iterator[None]:
        """Count one listener (and its pane interest) for as long as the
        block runs: the poller works only while the count is above zero."""
        with self._cond:
            self._subscribers += 1
            self._pane_interest.update(panes)
        try:
            yield
        finally:
            with self._cond:
                self._subscribers -= 1
                self._pane_interest.subtract(panes)
                self._pane_interest = +self._pane_interest  # drop zero counts

    @property
    def subscribers(self) -> int:
        with self._cond:
            return self._subscribers

    def pane_interest(self) -> list[str]:
        with self._cond:
            return sorted(self._pane_interest)


def sse_frame(event: Event) -> bytes:
    """One SSE frame: ``id:``, ``event:``, ``data:`` and the blank line."""
    body = json.dumps(event.to_dict(), separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {body}\n\n".encode()


SSE_PING = b": ping\n\n"


# --- The poller ---------------------------------------------------------------


def config_stamp(config_path: str | None) -> str | None:
    """A digest of the config file's bytes, None when it cannot be read: two
    equal stamps mean the same config, so the poller reloads only on change.
    Content, not mtime, because an edit inside one timestamp tick must count."""
    from magent.paths import find_config

    try:
        data = find_config(config_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


@dataclass
class EventPoller:
    """Diffs successive fleet snapshots into events on ``bus``.

    The callables are the seams: ``rows_fn(include_pane)`` returns the
    current rows, ``fire_fn(session, hook_state)`` is the attention
    debounce, ``pane_fn(row)`` captures one configured session's pane (None:
    not readable now), and ``generation_fn()`` names the config the rows
    came from (a capture taken across a change is dropped). ``for_config``
    wires the real ones.

    A session first seen on a hook tick is announced (``session.added``)
    with its pane fields (``PANE_FIELDS``) as None, because a hook tick reads
    no pane; the next pane tick reports them as a ``session.state`` change.
    Only the poller thread touches the snapshot: nothing here is locked."""

    bus: EventBus
    rows_fn: Callable[[bool], list[SessionRow]]
    fire_fn: Callable[[str, str], bool]
    pane_fn: Callable[[SessionRow], PaneCapture | None]
    generation_fn: Callable[[], str | None] = lambda: None
    pane_every: int = PANE_EVERY_N_TICKS
    _snapshot: dict[str, dict[str, object]] = field(
        default_factory=dict, init=False, repr=False
    )
    _pane_hash: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _ticks: int = field(default=0, init=False, repr=False)

    @classmethod
    def for_config(cls, bus: EventBus, config_path: str | None) -> EventPoller:
        """The real seams: ``fleetview.rows`` through ONE long-lived engine
        (so the attention debounce survives between ticks) and the ONE typed
        config it was built from, with each liveness probe bounded by
        ``PROBE_TIMEOUT_S``; and the pane reader ``control.read_pane`` would
        pick for the row -- ``control.node_pane`` (with that same config) for a
        live node session, ``psmux.read_pane`` for a live local one, nothing
        for a session that is not live.

        The typed config is loaded on the first tick that has one and again
        whenever the file's bytes change (``config_stamp``, a digest read
        each time it is asked for), never on a tick that finds it unchanged:
        loading per tick would echo ``load_config``'s stderr warning once a
        second. The node rows are built from it, so a node project removed
        while another stays configured stops being listed -- and tailed --
        on the next tick. A changed config that does not load fails the tick
        (logged by ``run_poller``) rather than leaving the old one in force.
        The engine is built from the FIRST config and kept until the server
        restarts, so the staleness windows, debounce and node extra-stores
        are frozen there (a ``settings.attention`` edit reaches the stream
        only after a restart). ``generation_fn`` is ``config_stamp`` too: a
        capture is published only if the stamp still matches the one taken
        before the tick's rows were built, so a config edit that lands
        mid-tick drops that tick's captures instead of publishing a pane the
        edit just removed. ``fire_fn`` keys the engine's debounce map (``_last_fired``)
        by SESSION NAME, and nothing on this path ever prunes it (only
        ``AttentionEngine.transitions`` does, which ``fleetview.rows`` never
        calls): one entry per (session, state) ever paged, for the life of
        the serve.

        The engine is the POLLER THREAD's alone: ``should_fire`` mutates
        ``_last_fired`` with no lock, and ``poll`` holds per-node state. A
        request thread (Task 6's routes) must not be handed it; a route that
        needs rows builds its own engine or passes ``cfg=`` to
        ``fleetview.rows``."""
        from magent import control, fleetview, psmux

        def _stamp() -> str | None:
            return config_stamp(config_path)

        cfg: MagentConfig | None = None
        engine: AttentionEngine | None = None
        seen: str | None = None

        def _config() -> tuple[MagentConfig, AttentionEngine] | None:
            nonlocal cfg, engine, seen
            stamp = _stamp()
            if cfg is None or stamp != seen:
                # Stamped BEFORE the read: an edit landing in between makes
                # the next call load again, never keeps a stale config.
                cfg = fleetview.load_typed(config_path)
                seen = stamp
            if cfg is None:
                return None
            if engine is None:
                engine = fleetview.engine_from_config(cfg)
            return cfg, engine

        def _rows(include_pane: bool) -> list[SessionRow]:
            now_cfg, now_engine = _config() or (None, None)
            return fleetview.rows(
                config_path,
                include_pane=include_pane,
                engine=now_engine,
                cfg=now_cfg,
                probe_timeout_s=PROBE_TIMEOUT_S,
            )

        def _fire(session: str, state: str) -> bool:
            got = _config()
            if got is None:
                return False
            return got[1].should_fire(session, state)

        def _pane(row: SessionRow) -> PaneCapture | None:
            if row.node_state is not None:
                got = _config()
                if row.node_state != "live" or got is None:
                    return None
                try:
                    text = control.node_pane(got[0], row)
                except control.ControlError:
                    return None
                return psmux.PaneCapture(text=text, timed_out=False)
            if not row.live:
                return None
            return psmux.read_pane(row.session)

        return cls(
            bus=bus,
            rows_fn=_rows,
            fire_fn=_fire,
            pane_fn=_pane,
            generation_fn=_stamp,
        )

    def tick(self) -> None:
        """One poll: hooks every tick, panes every ``pane_every`` ticks,
        subscribed pane tails every tick. A subscribed name is tailed only
        while this tick's rows hold it -- the configured fleet, read afresh
        -- and through its own row, so no raw subscriber string ever reaches
        a reader; and a capture is published only while ``generation_fn``
        still answers what it did before those rows were built. The
        pane-hash memory is pruned to the panes somebody still asks for, so
        an unsubscribed pane is tailed afresh when interest returns."""
        with_pane = self._ticks % self.pane_every == 0
        self._ticks += 1
        generation = self.generation_fn()
        rows = self.rows_fn(with_pane)
        self._diff(rows, with_pane=with_pane)
        configured = {r.session: r for r in rows}
        interest = self.bus.pane_interest()
        for session in interest:
            row = configured.get(session)
            if row is not None:
                self._tail(row, generation)
        self._pane_hash = {s: h for s, h in self._pane_hash.items() if s in interest}

    def _diff(self, rows: list[SessionRow], *, with_pane: bool) -> None:
        fields = HOOK_FIELDS + PANE_FIELDS if with_pane else HOOK_FIELDS
        current = {r.session: r.to_dict() for r in rows}
        for session, wire in current.items():
            before = self._snapshot.get(session)
            if before is None:
                # Separate copies: the snapshot is updated in place on later
                # ticks and must never reach into an event already published.
                self._snapshot[session] = dict(wire)
                self.bus.publish("session.added", dict(wire))
                continue
            changed = {k: wire[k] for k in fields if wire[k] != before.get(k)}
            if not changed:
                continue
            before.update(changed)
            self.bus.publish("session.state", {"session": session, **changed})
            state = changed.get("hook_state")
            if isinstance(state, str) and state in ATTENTION_STATES:
                self.bus.publish(
                    "attention",
                    {
                        "session": session,
                        "hook_state": state,
                        "fire": self.fire_fn(session, state),
                    },
                )
        for session in [s for s in self._snapshot if s not in current]:
            del self._snapshot[session]
            self._pane_hash.pop(session, None)
            self.bus.publish("session.removed", {"session": session})

    def _tail(self, row: SessionRow, generation: str | None) -> None:
        session = row.session
        capture = self.pane_fn(row)
        if capture is None or capture.timed_out:
            return
        if self.generation_fn() != generation:
            return  # the config changed under this tick: its rows are stale
        digest = hashlib.sha1(
            capture.text.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]
        if self._pane_hash.get(session) == digest:
            return
        self._pane_hash[session] = digest
        lines = capture.text.rstrip().splitlines()
        self.bus.publish(
            "session.pane",
            {
                "session": session,
                "text": capture.text,
                "hash": digest,
                "lines": len(lines),
            },
        )


def events_enabled() -> bool:
    """Whether ``MAGENT_EVENTS`` lets serve run the poller. Fail-open on an
    env that no longer validates, like every serve supervisor: the poller
    only reads."""
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env().events
    except ValidationError:
        get_logger("events").warning(
            "events: environment did not validate; polling anyway"
        )
        return True


def run_poller(
    poller: EventPoller,
    stop_event: threading.Event,
    interval: float = HOOK_INTERVAL_S,
) -> None:
    """The serve thread: tick while somebody listens, idle otherwise. A tick
    that raises is a log line and another try next interval -- the poller
    must never take down the server it rides on. Only the FIRST failure of a
    run is logged with its traceback; the repeats are counted silently until
    a tick succeeds, which logs one recovery line with the count (a dead
    multiplexer would otherwise write a traceback a second). A tick that
    takes longer than ``SLOW_TICK_FACTOR`` intervals (a slow multiplexer, a
    fleet too big for the budget) is reported the same way: one WARNING when
    the streak starts, one INFO once ``SLOW_TICK_CLEAR_TICKS`` consecutive
    ticks fit again (a single fitting tick between two slow ones ends
    nothing)."""
    log = get_logger("events")
    failed = 0
    slow = 0
    fitting = 0
    budget = SLOW_TICK_FACTOR * interval
    while not stop_event.is_set():
        if poller.bus.subscribers > 0:
            started = time.monotonic()
            try:
                poller.tick()
            except Exception:
                failed += 1
                if failed == 1:
                    log.exception("events: poll failed")
            else:
                if failed:
                    log.info("events: poller recovered after %d failed ticks", failed)
                    failed = 0
                took = time.monotonic() - started
                if took > budget:
                    slow += 1
                    fitting = 0
                    if slow == 1:
                        log.warning(
                            "events: tick took %.2fs, over %gx the %.2fs interval",
                            took,
                            SLOW_TICK_FACTOR,
                            interval,
                        )
                elif slow:
                    fitting += 1
                    if fitting >= SLOW_TICK_CLEAR_TICKS:
                        log.info(
                            "events: ticks fit the interval again after %d slow", slow
                        )
                        slow = 0
                        fitting = 0
        if stop_event.wait(interval):
            return
