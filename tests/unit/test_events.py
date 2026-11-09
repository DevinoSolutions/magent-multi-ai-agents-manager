"""events: the ring buffer, resume semantics, and the fleet-diff poller."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import env, events, log
from magent.fleetview import SessionRow
from magent.psmux import PaneCapture

if TYPE_CHECKING:
    from collections.abc import Callable


def _row(session: str, **changes: object) -> SessionRow:
    base = SessionRow(
        session=session,
        name=session,
        path=f"/w/{session}",
        cwd=f"/w/{session}",
        group=None,
        tool="claude",
        enabled=True,
        node=None,
        live=True,
        hook_state=None,
        hook_state_ts=None,
        hook_state_age_s=None,
        hook_state_stale=False,
        pane_state="idle",
        pane_state_ts=1.0,
        node_state=None,
        model=None,
        effort=None,
        session_id=None,
    )
    return dataclasses.replace(base, **changes)


class TestEventBus:
    def test_ids_are_epoch_and_a_counter(self):
        bus = events.EventBus("abcd1234")
        first = bus.publish("upload", {"n": 1})
        second = bus.publish("upload", {"n": 2})
        assert (first.id, second.id) == ("abcd1234:1", "abcd1234:2")
        assert bus.last_id() == "abcd1234:2"

    def test_publish_copies_its_data(self):
        bus = events.EventBus("e")
        data: dict[str, object] = {"n": 1}
        event = bus.publish("upload", data)
        data["n"] = 2
        assert event.data == {"n": 1}
        assert bus.since("e:0").events[0].data == {"n": 1}

    def test_a_fresh_epoch_is_eight_hex_chars(self):
        epoch = events.EventBus().epoch
        assert len(epoch) == 8
        int(epoch, 16)

    def test_since_returns_only_newer_events(self):
        bus = events.EventBus("e")
        bus.publish("upload", {"n": 1})
        mark = bus.last_id()
        bus.publish("upload", {"n": 2})
        got = bus.since(mark)
        assert [e.data for e in got.events] == [{"n": 2}]
        assert got.next == "e:2"
        assert got.reset is None

    def test_since_none_starts_from_now(self):
        bus = events.EventBus("e")
        bus.publish("upload", {})
        assert bus.since(None) == events.Since(events=[], next="e:1")

    def test_a_foreign_epoch_is_a_reset(self):
        bus = events.EventBus("e")
        assert bus.since("other:3").reset == "epoch"

    def test_a_garbled_id_is_a_reset(self):
        assert events.EventBus("e").since("nonsense").reset == "epoch"

    def test_an_id_from_the_future_is_a_reset(self):
        assert events.EventBus("e").since("e:9").reset == "epoch"

    def test_falling_out_of_the_ring_is_evicted(self):
        bus = events.EventBus("e", ring=3)
        for n in range(6):
            bus.publish("upload", {"n": n})
        assert bus.since("e:1").reset == "evicted"
        assert [e.data["n"] for e in bus.since("e:3").events] == [3, 4, 5]

    def test_wait_returns_at_once_when_events_are_waiting(self):
        bus = events.EventBus("e")
        bus.publish("upload", {})
        started = time.monotonic()
        assert len(bus.wait("e:0", timeout=5).events) == 1
        assert time.monotonic() - started < 1

    def test_wait_times_out_empty(self):
        got = events.EventBus("e").wait("e:0", timeout=0.05)
        assert got.events == []
        assert got.reset is None

    def test_wait_wakes_on_publish(self):
        bus = events.EventBus("e")
        threading.Timer(0.1, lambda: bus.publish("upload", {"late": True})).start()
        got = bus.wait("e:0", timeout=5)
        assert [e.data for e in got.events] == [{"late": True}]

    def test_wait_from_now_wakes_on_publish(self):
        bus = events.EventBus("e")
        bus.publish("upload", {"old": True})
        threading.Timer(0.1, lambda: bus.publish("upload", {"late": True})).start()
        got = bus.wait(None, timeout=5)
        assert [e.data for e in got.events] == [{"late": True}]
        assert got.next == "e:2"

    def test_subscription_counts_listeners_and_pane_interest(self):
        bus = events.EventBus("e")
        with bus.subscription(("a", "b")):
            with bus.subscription(("a",)):
                assert bus.subscribers == 2
                assert bus.pane_interest() == ["a", "b"]
            assert bus.pane_interest() == ["a", "b"]
        assert bus.subscribers == 0
        assert bus.pane_interest() == []

    def test_sse_frame_shape(self):
        event = events.Event(id="e:1", type="upload", ts=2.0, data={"x": 1})
        frame = events.sse_frame(event).decode()
        assert frame.startswith("id: e:1\nevent: upload\ndata: ")
        assert frame.endswith("\n\n")
        body = frame.split("data: ", 1)[1].strip()
        assert json.loads(body) == {
            "id": "e:1",
            "type": "upload",
            "ts": 2.0,
            "data": {"x": 1},
        }


class _Fleet:
    """Scripted seams for EventPoller."""

    def __init__(self, rows: list[SessionRow]) -> None:
        self.rows = rows
        self.fired: list[tuple[str, str]] = []
        self.pane = PaneCapture(text="hello\n", timed_out=False)
        self.read: list[SessionRow] = []

    def rows_fn(self, _include_pane: bool) -> list[SessionRow]:
        return list(self.rows)

    def fire_fn(self, session: str, state: str) -> bool:
        self.fired.append((session, state))
        return True

    def pane_fn(self, row: SessionRow) -> PaneCapture:
        self.read.append(row)
        return self.pane


def _poller(fleet: _Fleet, bus: events.EventBus) -> events.EventPoller:
    return events.EventPoller(
        bus=bus, rows_fn=fleet.rows_fn, fire_fn=fleet.fire_fn, pane_fn=fleet.pane_fn
    )


class TestEventPoller:
    def test_first_tick_announces_every_session(self):
        bus = events.EventBus("e")
        _poller(_Fleet([_row("a"), _row("b")]), bus).tick()
        got = bus.since("e:0").events
        assert [(e.type, e.data["session"]) for e in got] == [
            ("session.added", "a"),
            ("session.added", "b"),
        ]

    def test_session_added_carries_the_full_row(self):
        bus = events.EventBus("e")
        _poller(_Fleet([_row("a")]), bus).tick()
        (added,) = bus.since("e:0").events
        assert set(added.data) == {f.name for f in dataclasses.fields(SessionRow)}
        assert added.data == _row("a").to_dict()

    def test_a_replayed_added_event_keeps_its_publish_time_fields(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        poller.tick()
        fleet.rows = [_row("a", hook_state="working")]
        poller.tick()
        added, state = bus.since("e:0").events
        assert added.type == "session.added"
        assert added.data["hook_state"] is None  # not the later "working"
        assert state.data == {"session": "a", "hook_state": "working"}

    def test_an_unchanged_row_is_not_re_reported(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        poller.tick()
        mark = bus.last_id()
        fleet.rows = [_row("a", hook_state="working")]
        poller.tick()
        poller.tick()
        assert [e.type for e in bus.since(mark).events] == ["session.state"]

    def test_a_hook_change_is_a_state_event_and_an_attention_event(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        poller.tick()
        mark = bus.last_id()
        fleet.rows = [_row("a", hook_state="needs-input")]
        poller.tick()
        got = bus.since(mark).events
        assert [e.type for e in got] == ["session.state", "attention"]
        assert got[0].data == {"session": "a", "hook_state": "needs-input"}
        assert got[1].data == {
            "session": "a",
            "hook_state": "needs-input",
            "fire": True,
        }
        assert fleet.fired == [("a", "needs-input")]

    def test_working_is_a_state_change_but_not_attention(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        poller.tick()
        mark = bus.last_id()
        fleet.rows = [_row("a", hook_state="working")]
        poller.tick()
        assert [e.type for e in bus.since(mark).events] == ["session.state"]

    def test_pane_fields_are_compared_only_on_pane_ticks(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        poller.pane_every = 2
        poller.tick()  # tick 0: pane tick
        mark = bus.last_id()
        fleet.rows = [_row("a", pane_state=None)]  # what a hook-only read shows
        poller.tick()  # tick 1: hook tick -> pane_state ignored
        assert bus.since(mark).events == []
        fleet.rows = [_row("a", pane_state="busy")]
        poller.tick()  # tick 2: pane tick
        assert [e.data for e in bus.since(mark).events] == [
            {"session": "a", "pane_state": "busy"}
        ]

    def test_a_vanished_session_is_removed(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a"), _row("b")])
        poller = _poller(fleet, bus)
        poller.tick()
        mark = bus.last_id()
        fleet.rows = [_row("a")]
        poller.tick()
        assert [(e.type, e.data) for e in bus.since(mark).events] == [
            ("session.removed", {"session": "b"})
        ]

    def test_subscribed_panes_are_tailed_once_per_change(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        with bus.subscription(("a",)):
            poller.tick()
            poller.tick()  # same text: no second pane event
            fleet.pane = PaneCapture(text="hello\nworld\n", timed_out=False)
            poller.tick()
        panes = [e.data for e in bus.since("e:0").events if e.type == "session.pane"]
        assert [p["text"] for p in panes] == ["hello\n", "hello\nworld\n"]
        assert panes[1]["lines"] == 2
        assert panes[0]["hash"] != panes[1]["hash"]

    def test_a_timed_out_capture_publishes_nothing(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        fleet.pane = PaneCapture(text="", timed_out=True)
        with bus.subscription(("a",)):
            _poller(fleet, bus).tick()
        assert not [e for e in bus.since("e:0").events if e.type == "session.pane"]

    def test_a_pane_is_read_through_its_configured_row(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        with bus.subscription(("a",)):
            _poller(fleet, bus).tick()
        assert fleet.read == [_row("a")]

    def test_a_name_outside_the_config_is_never_read(self):
        # The API refuses such a subscription up front; the poller must not
        # rely on that, or a raw name reaches psmux and its pane is published.
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        with bus.subscription(("not-in-config",)):
            _poller(fleet, bus).tick()
        assert fleet.read == []
        assert not [e for e in bus.since("e:0").events if e.type == "session.pane"]

    def test_a_session_dropped_from_the_config_stops_being_read(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        with bus.subscription(("a",)):
            poller.tick()
            fleet.rows = []
            fleet.pane = PaneCapture(text="private\n", timed_out=False)
            poller.tick()
        assert len(fleet.read) == 1
        panes = [e.data for e in bus.since("e:0").events if e.type == "session.pane"]
        assert [p["text"] for p in panes] == ["hello\n"]

    def test_a_row_the_reader_declines_publishes_nothing(self):
        bus = events.EventBus("e")
        poller = events.EventPoller(
            bus=bus,
            rows_fn=lambda _p: [_row("a")],
            fire_fn=lambda *_: False,
            pane_fn=lambda _row: None,
        )
        with bus.subscription(("a",)):
            poller.tick()
        assert not [e for e in bus.since("e:0").events if e.type == "session.pane"]

    def test_an_unsubscribed_pane_is_forgotten(self):
        bus = events.EventBus("e")
        poller = _poller(_Fleet([_row("a")]), bus)
        with bus.subscription(("a",)):
            poller.tick()
            assert "a" in poller._pane_hash
        poller.tick()
        assert "a" not in poller._pane_hash


class TestRunPoller:
    def test_idles_without_subscribers_and_ticks_with_one(self):
        bus = events.EventBus("e")
        fleet = _Fleet([_row("a")])
        poller = _poller(fleet, bus)
        stop = threading.Event()
        thread = threading.Thread(
            target=events.run_poller, args=(poller, stop, 0.01), daemon=True
        )
        thread.start()
        time.sleep(0.1)
        assert bus.last_id() == "e:0"  # nobody listening: no tick
        with bus.subscription():
            deadline = time.monotonic() + 5
            while bus.last_id() == "e:0" and time.monotonic() < deadline:
                time.sleep(0.01)
        stop.set()
        thread.join(5)
        assert bus.last_id() != "e:0"
        assert not thread.is_alive()

    def test_ticking_stops_after_the_last_subscriber_leaves(self):
        bus = events.EventBus("e")
        calls: list[int] = []

        def _rows(_include_pane: bool) -> list[SessionRow]:
            calls.append(1)
            return []

        poller = events.EventPoller(
            bus=bus, rows_fn=_rows, fire_fn=lambda *_: False, pane_fn=lambda _s: None
        )
        stop = threading.Event()
        thread = threading.Thread(
            target=events.run_poller, args=(poller, stop, 0.01), daemon=True
        )
        thread.start()
        with bus.subscription():
            deadline = time.monotonic() + 5
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
        time.sleep(0.05)  # let a tick already in flight finish
        settled = len(calls)
        time.sleep(0.1)
        stop.set()
        thread.join(5)
        assert settled >= 1
        assert len(calls) == settled

    def test_a_crashing_tick_is_logged_and_survived(self, caplog):
        bus = events.EventBus("e")
        stop = threading.Event()
        calls: list[int] = []

        def _boom(_include_pane: bool) -> list[SessionRow]:
            calls.append(1)
            if len(calls) == 3:
                stop.set()  # the loop returns after this tick's wait
            raise RuntimeError("psmux exploded")

        poller = events.EventPoller(
            bus=bus, rows_fn=_boom, fire_fn=lambda *_: False, pane_fn=lambda _s: None
        )
        log.get_logger("events")  # configure first: get_logger sets the level
        caplog.set_level(logging.INFO, logger="magent.events")
        with bus.subscription():
            # A wide enough interval that a trivial tick on a loaded box can
            # never count as slow (the slow-tick warning has its own test).
            events.run_poller(poller, stop, 0.05)
        assert len(calls) == 3
        records = [r for r in caplog.records if r.name == "magent.events"]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("ERROR", "events: poll failed")
        ]
        assert records[0].exc_info is not None
        assert "psmux exploded" in caplog.text

    def test_a_recovered_poller_says_so_once(self, caplog):
        bus = events.EventBus("e")
        stop = threading.Event()
        calls: list[int] = []

        def _flaky(_include_pane: bool) -> list[SessionRow]:
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("psmux exploded")
            if len(calls) == 4:
                stop.set()
            return []

        poller = events.EventPoller(
            bus=bus, rows_fn=_flaky, fire_fn=lambda *_: False, pane_fn=lambda _s: None
        )
        log.get_logger("events")
        caplog.set_level(logging.INFO, logger="magent.events")
        with bus.subscription():
            events.run_poller(poller, stop, 0.05)
        assert len(calls) == 4
        records = [r for r in caplog.records if r.name == "magent.events"]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("ERROR", "events: poll failed"),
            ("INFO", "events: poller recovered after 2 failed ticks"),
        ]

    def _slow_ticks(self, caplog, script: str) -> list[logging.LogRecord]:
        """Run one tick per letter of ``script`` ('s' slow: 150ms against a
        50ms interval, 100ms budget; 'f' fast) and return the events log."""
        bus = events.EventBus("e")
        stop = threading.Event()
        calls: list[int] = []

        def _rows(_include_pane: bool) -> list[SessionRow]:
            calls.append(1)
            if script[len(calls) - 1] == "s":
                time.sleep(0.15)
            if len(calls) == len(script):
                stop.set()
            return []

        poller = events.EventPoller(
            bus=bus, rows_fn=_rows, fire_fn=lambda *_: False, pane_fn=lambda _s: None
        )
        log.get_logger("events")
        caplog.set_level(logging.INFO, logger="magent.events")
        with bus.subscription():
            events.run_poller(poller, stop, 0.05)
        assert len(calls) == len(script)
        return [r for r in caplog.records if r.name == "magent.events"]

    def test_a_slow_streak_is_one_warning_and_one_all_clear(self, caplog):
        # Two slow ticks are one streak (one WARNING, not one a tick); the
        # all-clear comes after SLOW_TICK_CLEAR_TICKS fitting ticks in a row.
        records = self._slow_ticks(caplog, "ssfff")
        assert [r.levelname for r in records] == ["WARNING", "INFO"]
        assert records[0].getMessage().startswith("events: tick took ")
        assert "over 2x the 0.05s interval" in records[0].getMessage()
        assert records[1].getMessage() == (
            "events: ticks fit the interval again after 2 slow"
        )

    def test_fewer_fitting_ticks_than_the_hysteresis_is_no_all_clear(self, caplog):
        records = self._slow_ticks(caplog, "ssff")
        assert [r.levelname for r in records] == ["WARNING"]

    def test_a_slow_tick_inside_the_hysteresis_window_extends_the_streak(self, caplog):
        # s s f s f f f: the lone fitting tick ends nothing, the later slow
        # tick joins the same streak (no second WARNING), and the count the
        # all-clear reports is every slow tick of the streak.
        records = self._slow_ticks(caplog, "ssfsfff")
        assert [r.levelname for r in records] == ["WARNING", "INFO"]
        assert records[1].getMessage() == (
            "events: ticks fit the interval again after 3 slow"
        )

    def test_the_hysteresis_is_three_ticks(self):
        assert events.SLOW_TICK_CLEAR_TICKS == 3


class TestForConfig:
    """The real seams: one typed load and one engine for the life of the
    poller, handed to every ``fleetview.rows`` call with the bounded probe."""

    @pytest.fixture
    def seams(self, monkeypatch):
        from magent import fleetview

        loads: list[str | None] = []
        rows_calls: list[dict[str, object]] = []
        cfg = object()
        engine = _Engine()

        def _load(path):
            loads.append(path)
            return cfg

        monkeypatch.setattr(fleetview, "load_typed", _load)
        monkeypatch.setattr(fleetview, "engine_from_config", lambda _cfg: engine)
        monkeypatch.setattr(
            fleetview, "rows", lambda path, **kw: rows_calls.append(kw) or []
        )
        return loads, rows_calls, cfg, engine

    def test_loads_the_config_once_and_reuses_the_engine(self, seams):
        loads, rows_calls, cfg, engine = seams
        poller = events.EventPoller.for_config(events.EventBus("e"), "/c.json")

        poller.rows_fn(False)
        poller.rows_fn(True)
        poller.fire_fn("a", "done")

        assert loads == ["/c.json"]
        assert rows_calls == [
            {
                "include_pane": False,
                "engine": engine,
                "cfg": cfg,
                "probe_timeout_s": events.PROBE_TIMEOUT_S,
            },
            {
                "include_pane": True,
                "engine": engine,
                "cfg": cfg,
                "probe_timeout_s": events.PROBE_TIMEOUT_S,
            },
        ]
        assert engine.fired == [("a", "done")]

    def test_no_config_yet_polls_without_an_engine_and_never_fires(
        self, seams, monkeypatch
    ):
        from magent import fleetview

        loads, rows_calls, _cfg, _engine = seams
        monkeypatch.setattr(fleetview, "load_typed", loads.append)  # -> None
        poller = events.EventPoller.for_config(events.EventBus("e"), None)

        poller.rows_fn(False)
        fired = poller.fire_fn("a", "done")
        poller.rows_fn(False)

        assert fired is False
        # Retried by every row build (a config may appear); fire never loads.
        assert loads == [None, None]
        assert rows_calls[0]["engine"] is None
        assert rows_calls[0]["cfg"] is None
        assert rows_calls[0]["probe_timeout_s"] == events.PROBE_TIMEOUT_S

    def test_a_live_local_row_is_read_through_psmux(self, seams, monkeypatch):
        from magent import psmux

        reads: list[str] = []
        capture = PaneCapture(text="x\n", timed_out=False)
        monkeypatch.setattr(
            psmux, "read_pane", lambda session: reads.append(session) or capture
        )
        poller = events.EventPoller.for_config(events.EventBus("e"), None)
        assert poller.pane_fn(_row("a")) is capture
        assert poller.pane_fn(_row("b", live=False)) is None
        assert reads == ["a"]

    def test_a_live_node_row_is_read_on_its_node_with_the_kept_config(
        self, seams, monkeypatch
    ):
        from magent import control, psmux

        _loads, _rows_calls, cfg, _engine = seams
        dialled: list[tuple[object, str]] = []

        def _node_pane(got_cfg, row):
            dialled.append((got_cfg, row.session))
            return "remote\n"

        def _no_local(_session):
            raise AssertionError("a node session was read through local psmux")

        monkeypatch.setattr(control, "node_pane", _node_pane)
        monkeypatch.setattr(psmux, "read_pane", _no_local)
        poller = events.EventPoller.for_config(events.EventBus("e"), "/c.json")
        poller.rows_fn(False)  # the tick's row build loads the config the read uses
        node = {"node": "box", "pane_state": None}
        live = poller.pane_fn(_row("r1", node_state="live", **node))
        stale = poller.pane_fn(_row("r2", node_state="stale", live=False, **node))
        assert live == PaneCapture(text="remote\n", timed_out=False)
        assert stale is None
        assert dialled == [(cfg, "r1")]

    def test_a_node_read_that_fails_is_declined(self, seams, monkeypatch):
        from magent import control

        def _refuse(_cfg, row):
            raise control.ControlError("unavailable", f"could not read {row.session}")

        monkeypatch.setattr(control, "node_pane", _refuse)
        poller = events.EventPoller.for_config(events.EventBus("e"), "/c.json")
        poller.rows_fn(False)
        row = _row("r1", node="box", node_state="live", pane_state=None)
        assert poller.pane_fn(row) is None

    def test_fire_and_pane_never_load_the_config_themselves(self, seams, monkeypatch):
        from magent import control, fleetview

        loads, _rows_calls, cfg, engine = seams
        monkeypatch.setattr(control, "node_pane", lambda got, _row: "x\n")
        poller = events.EventPoller.for_config(events.EventBus("e"), "/c.json")
        poller.rows_fn(False)

        def _no_load(_path):
            raise AssertionError("a callback reloaded the config")

        monkeypatch.setattr(fleetview, "load_typed", _no_load)
        monkeypatch.setattr(events, "config_stamp", _no_load)
        assert poller.fire_fn("a", "done") is True
        row = _row("r1", node="box", node_state="live", pane_state=None)
        assert poller.pane_fn(row) == PaneCapture(text="x\n", timed_out=False)
        assert loads == ["/c.json"]
        assert engine.fired == [("a", "done")]
        assert cfg is not None

    def test_the_probe_bound_is_a_few_seconds(self):
        assert 1.0 <= events.PROBE_TIMEOUT_S <= 10.0


class TestForConfigFollowsTheConfig:
    """The real wiring end to end -- ``for_config``, ``fleetview.rows``, the
    node rows from the sync daemon's files, ``control.node_pane`` -- with only
    the ssh capture (``remote_mux.capture_pane``) substituted."""

    @pytest.fixture
    def two_nodes(self, tmp_path, monkeypatch):
        from magent import nodes, psmux, remote_mux
        from magent.nodes import NodeMapEntry

        monkeypatch.setattr(psmux, "find_psmux", lambda: None)
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        nodes.write_json_atomic(
            nodes.sessions_path("second"),
            {"ts": time.time(), "sessions": ["a", "b"]},
        )
        for name in ("a", "b"):
            nodes.update_node_map(
                name,
                NodeMapEntry(
                    nick="second",
                    sid=name,
                    placed_ts=1.0,
                    attached_existing=False,
                    remote_root=f"~/magent/{name}",
                    target="demo@box-second",
                    cwd=f"/home/demo/magent/{name}",
                ),
            )
        path = tmp_path / "magent.config.json"

        def write(names: list[str]) -> None:
            path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "projects": [
                            {"path": str(tmp_path / n), "title": n, "node": "second"}
                            for n in names
                        ],
                        "settings": {
                            "nodes": {"second": {"host": "box-second", "user": "demo"}}
                        },
                    }
                ),
                encoding="utf-8",
            )

        write(["a", "b"])
        captured: list[str] = []
        texts: dict[str, str] = {"a": "a before\n", "b": "b before\n"}
        hooks: list[Callable[[str], None]] = []

        def _capture(_node, session):
            captured.append(session)
            for hook in hooks:
                hook(session)
            return texts[session]

        monkeypatch.setattr(remote_mux, "capture_pane", _capture)
        return str(path), write, captured, texts, hooks

    @staticmethod
    def _panes(bus: events.EventBus) -> list[tuple[object, object]]:
        return [
            (e.data["session"], e.data["text"])
            for e in bus.since(f"{bus.epoch}:0").events
            if e.type == "session.pane"
        ]

    def test_a_removed_node_project_stops_being_read_while_another_stays(
        self, two_nodes
    ):
        path, write, captured, texts, _hooks = two_nodes
        bus = events.EventBus("e")
        poller = events.EventPoller.for_config(bus, path)
        with bus.subscription(("a", "b")):
            poller.tick()
            assert sorted(captured) == ["a", "b"]
            write(["b"])
            texts.update(a="a private AFTER removal\n", b="b after\n")
            captured.clear()
            poller.tick()
            poller.tick()
        assert captured == ["b", "b"]
        assert sorted(self._panes(bus)) == [
            ("a", "a before\n"),
            ("b", "b after\n"),
            ("b", "b before\n"),
        ]
        removed = [
            e.data for e in bus.since("e:0").events if e.type == "session.removed"
        ]
        assert removed == [{"session": "a"}]

    def test_an_unchanged_config_is_loaded_once(self, two_nodes, monkeypatch):
        from magent import fleetview

        path, _write, _captured, _texts, _hooks = two_nodes
        loads: list[object] = []
        real = fleetview.load_typed
        monkeypatch.setattr(
            fleetview, "load_typed", lambda p: loads.append(p) or real(p)
        )
        bus = events.EventBus("e")
        poller = events.EventPoller.for_config(bus, path)
        with bus.subscription(("a",)):
            for _ in range(3):
                poller.tick()
        assert loads == [path]

    def test_an_unreadable_config_mid_tick_never_loses_an_attention_event(
        self, two_nodes, tmp_path, monkeypatch
    ):
        """Both node sessions go idle -> needs-input on the tick where an
        external truncate-and-write leaves the config empty right after the
        engine's poll (the rows are built by then). Each transition still
        owes its ``attention`` event: emitted on that tick, or, the tick
        failing, on the one after the file is whole again -- never lost
        because ``_diff`` already committed the new hook state."""
        from magent import fleetview, nodes

        path, _write, _captured, _texts, _hooks = two_nodes
        whole = Path(path).read_bytes()

        def _record(name: str, state: str) -> None:
            store = nodes.state_dir("second", name)
            store.mkdir(parents=True, exist_ok=True)
            (store / f"{name}.json").write_text(
                json.dumps(
                    {
                        "state": state,
                        "ts": time.time(),
                        "cwd": f"/home/demo/magent/{name}",
                        "session_id": f"s-{name}",
                    }
                ),
                encoding="utf-8",
            )

        truncate_after_poll = [False]
        real_engine = fleetview.engine_from_config

        def _engine(cfg):
            engine = real_engine(cfg)
            real_poll = engine.poll

            def _poll():
                views = real_poll()
                if truncate_after_poll[0]:
                    truncate_after_poll[0] = False
                    Path(path).write_bytes(b"")
                return views

            monkeypatch.setattr(engine, "poll", _poll)
            return engine

        monkeypatch.setattr(fleetview, "engine_from_config", _engine)
        for name in ("a", "b"):
            _record(name, "idle")
        bus = events.EventBus("e")
        poller = events.EventPoller.for_config(bus, path)
        with bus.subscription():
            poller.tick()
            assert [
                (e.data["session"], e.data["hook_state"])
                for e in bus.since("e:0").events
                if e.type == "session.added"
            ] == [("a", "idle"), ("b", "idle")]
            for name in ("a", "b"):
                _record(name, "needs-input")
            truncate_after_poll[0] = True
            with contextlib.suppress(Exception):
                poller.tick()  # a failing tick is run_poller's to log
            Path(path).write_bytes(whole)
            poller.tick()
            poller.tick()
        attention = [
            (e.data["session"], e.data["hook_state"], e.data["fire"])
            for e in bus.since("e:0").events
            if e.type == "attention"
        ]
        assert sorted(attention) == [
            ("a", "needs-input", True),
            ("b", "needs-input", True),
        ]

    def test_a_capture_taken_across_a_config_edit_is_not_published(self, two_nodes):
        # The rows were built, then the edit removed `a` while its pane was
        # being read: that capture must be dropped, not published once.
        path, write, captured, texts, hooks = two_nodes
        bus = events.EventBus("e")
        poller = events.EventPoller.for_config(bus, path)
        texts["a"] = "a private AFTER removal\n"

        def _remove_a(session: str) -> None:
            if session == "a":
                write(["b"])

        hooks.append(_remove_a)
        with bus.subscription(("a",)):
            poller.tick()
            hooks.clear()
            poller.tick()
        assert captured == ["a"]
        assert self._panes(bus) == []


class _Engine:
    def __init__(self) -> None:
        self.fired: list[tuple[str, str]] = []

    def should_fire(self, session: str, state: str) -> bool:
        self.fired.append((session, state))
        return True


class TestOptOut:
    def test_pinned_off_for_every_test(self):
        assert events.events_enabled() is False

    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("MAGENT_EVENTS")
        monkeypatch.setattr(env, "_cached_env", None)
        assert events.events_enabled() is True

    def test_a_broken_env_fails_open(self, monkeypatch):
        monkeypatch.setenv("MAGENT_EVENTS", "not-a-bool")
        monkeypatch.setattr(env, "_cached_env", None)
        assert events.events_enabled() is True


@pytest.mark.parametrize("field", events.HOOK_FIELDS + events.PANE_FIELDS)
def test_diffed_fields_are_real_row_fields(field):
    assert field in {f.name for f in dataclasses.fields(SessionRow)}
