"""events: the ring buffer, resume semantics, and the fleet-diff poller."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time

import pytest

from magent import env, events, log
from magent.fleetview import SessionRow
from magent.psmux import PaneCapture


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

    def rows_fn(self, _include_pane: bool) -> list[SessionRow]:
        return list(self.rows)

    def fire_fn(self, session: str, state: str) -> bool:
        self.fired.append((session, state))
        return True

    def pane_fn(self, _session: str) -> PaneCapture:
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
            events.run_poller(poller, stop, 0.001)
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
            events.run_poller(poller, stop, 0.001)
        assert len(calls) == 4
        records = [r for r in caplog.records if r.name == "magent.events"]
        assert [(r.levelname, r.getMessage()) for r in records] == [
            ("ERROR", "events: poll failed"),
            ("INFO", "events: poller recovered after 2 failed ticks"),
        ]


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
