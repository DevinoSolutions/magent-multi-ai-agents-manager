"""The link's event forwarder: daemon events sealed per device, routing-only
meta, 250 ms batches split under 256 KiB and 500 events, oversize events
dropped, a reset answered with a fresh snapshot, rate limits waited out
through the runner's sleep, and a thread that never outlives its runner."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from magent import link, linkcrypto
from magent.link import EventForwarder, LinkRunner, RelayClient, event_meta
from magent.linkcrypto import AAD_EVT
from tests._fake_relay import FakeRelay
from tests.unit._link_fakes import (
    HOST_ID,
    OTHER_ID,
    SESSION_ROW,
    TABLET_ID,
    FakeFeed,
    FakeHost,
    Running,
    new_device,
    save_linked,
    stop_all,
    stray_forwarders,
    write_link,
)

EMOJI = chr(0x1F600)  # two UTF-16 code units


@pytest.fixture(autouse=True)
def _no_link_thread_outlives_its_test():
    """Torn down last (autouse), after the relay: no runner and no forwarder
    thread is left behind, whether the test passed or not."""
    yield
    assert stop_all() == [], "a link runner did not stop"
    assert stray_forwarders() == [], "an event forwarder did not stop"


@pytest.fixture
def relay():
    with FakeRelay() as fake:
        yield fake


@pytest.fixture
def phone():
    return new_device()


def _event(n: int, type_: str = "session.state", **data: object) -> dict[str, object]:
    return {
        "id": f"e0e0e0e0:{n}",
        "type": type_,
        "ts": 1_760_000_000.0 + n,
        "data": data,
    }


def _event_posts(relay: FakeRelay) -> int:
    return sum(
        1
        for method, path, _ in list(relay.requests)
        if (method, path) == ("POST", "/v1/host/events")
    )


def _online(tmp_path, relay, devices, host):
    linked = write_link(tmp_path, relay.url, devices, host_token=relay.host_token)
    run = Running(host, linked)
    relay.wait_for(run.runner.online)
    return linked, run


class TestMeta:
    def test_only_the_session_rides_in_the_clear(self):
        event = _event(
            1, "session.pane", session="magent-alpha", text="DECOY pane text"
        )
        assert event_meta(event) == {"session": "magent-alpha"}

    def test_a_session_change_carries_the_snapshot_s_routing_fields(self):
        # session.added/state feed the relay's session mirror from meta: the
        # same fields the snapshot already sends in the clear, camelCase. A
        # field the event lacks is left out; a state over 32 is null.
        added = _event(1, "session.added", **SESSION_ROW)
        assert event_meta(added) == {
            "session": "magent-alpha",
            "name": "alpha",
            "live": True,
            "hookState": "working",
            "paneState": "busy",
            "nodeState": None,
        }
        changed = _event(
            2,
            session="magent-alpha",
            hook_state="x" * 33,
            model="opus",
            session_id="DECOY-claude-session",
        )
        assert event_meta(changed) == {"session": "magent-alpha", "hookState": None}

    @pytest.mark.parametrize(
        ("name", "cut"),
        [
            (EMOJI * 150, EMOJI * 100),
            ("a" + EMOJI * 100, "a" + EMOJI * 99),  # the cut falls mid-pair
            (EMOJI * 100, EMOJI * 100),
        ],
        ids=["150-emoji", "mid-pair", "exactly-200"],
    )
    def test_a_name_is_cut_as_the_relay_counts(self, name, cut):
        # The relay counts UTF-16 code units: an emoji is two.
        meta = event_meta(_event(1, "session.added", session="a", name=name))
        assert meta["name"] == cut

    def test_a_state_is_measured_as_the_relay_counts(self):
        over = event_meta(_event(1, session="a", hook_state=EMOJI * 20))
        at = event_meta(_event(2, session="a", hook_state=EMOJI * 16))
        assert over["hookState"] is None  # 40 code units
        assert at["hookState"] == EMOJI * 16  # 32 code units
        page = event_meta(
            _event(3, "attention", session="a", hook_state=EMOJI * 20, fire=True)
        )
        assert page["hookState"] is None

    def test_attention_also_says_whether_to_page(self):
        event = _event(
            2, "attention", session="magent-alpha", hook_state="needs-input", fire=True
        )
        assert event_meta(event) == {
            "session": "magent-alpha",
            "fire": True,
            "hookState": "needs-input",
        }

    def test_no_other_type_carries_session_fields(self):
        event = _event(3, "upload", session="magent-alpha", name="photo.png")
        assert event_meta(event) == {"session": "magent-alpha"}


class TestForwarding:
    def test_an_event_is_sealed_for_each_device(self, tmp_path, relay, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        host = FakeHost()
        linked, run = _online(tmp_path, relay, (phone, tablet), host)
        event = _event(1, "session.pane", session="magent-alpha", text="DECOY pane")
        host.feed.push(event)

        relay.wait_for(lambda: relay.events)
        posted = relay.events[0]
        assert posted["daemonEventId"] == "e0e0e0e0:1"
        assert posted["type"] == "session.pane"
        assert posted["meta"] == {"session": "magent-alpha"}
        assert "DECOY" not in json.dumps(
            {k: v for k, v in posted.items() if k != "sealed"}
        )
        assert set(posted["sealed"]) == {phone.device_id, tablet.device_id}
        for device in (phone, tablet):
            plain = linkcrypto.open_sealed(
                linked.device_keys(device).h2d,
                posted["sealed"][device.device_id],
                f"{AAD_EVT}{HOST_ID}:e0e0e0e0:1",
            )
            assert json.loads(plain) == event
        run.finish(relay)

    def test_events_close_together_share_one_post(self, tmp_path, relay, phone):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        host.feed.push(_event(1, session="a"))
        time.sleep(0.1)  # inside the 250 ms window
        host.feed.push(_event(2, session="b"), _event(3, session="c"))

        relay.wait_for(lambda: len(relay.events) == 3)
        assert _event_posts(relay) == 1
        run.finish(relay)

    def test_a_big_batch_is_split_under_256_kib(self, tmp_path, relay, phone):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        host.feed.push(
            *[
                _event(n, "session.pane", session="a", text="x" * 60_000)
                for n in range(5)
            ]
        )

        relay.wait_for(lambda: len(relay.events) == 5)
        assert _event_posts(relay) >= 2  # every one accepted: none hit the fake's 413
        run.finish(relay)

    def test_a_batch_over_500_events_is_split(self, tmp_path, relay, phone):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        host.feed.push(*[_event(n, session="a") for n in range(600)])

        relay.wait_for(lambda: len(relay.events) == 600)
        assert _event_posts(relay) == 2  # the fake refuses a 501-event batch
        run.finish(relay)

    def test_an_event_too_big_alone_is_dropped_with_a_warning(
        self, tmp_path, relay, phone, caplog
    ):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        with caplog.at_level("WARNING", logger="magent.link"):
            host.feed.push(
                # Not a pane (a pane keeps its tail instead): nothing to cut.
                _event(1, session="a", note="x" * 300_000),
                _event(2, session="b"),
            )
            relay.wait_for(lambda: relay.events)
            time.sleep(0.3)
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:2"]
        assert any("dropped event e0e0e0e0:1" in r.getMessage() for r in caplog.records)
        run.finish(relay)

    @pytest.mark.parametrize(("devices", "chars"), [(1, 200_000), (3, 70_000)])
    def test_a_pane_too_big_for_one_post_keeps_its_tail(
        self, tmp_path, relay, phone, devices, chars
    ):
        # Sealed once per device, a pane of about 190 KB (one device) or
        # 64 KB (three) would pass the 256 KiB post: it keeps its last whole
        # lines instead of being dropped.
        paired = (
            phone,
            new_device(TABLET_ID, "Tablet DECOY"),
            new_device(OTHER_ID, "Other DECOY"),
        )[:devices]
        host = FakeHost()
        linked, run = _online(tmp_path, relay, paired, host)
        lines = [f"line {n:07d} DECOY" for n in range(chars // 18)]
        text = "\n".join(lines)
        host.feed.push(_event(1, "session.pane", session="a", text=text, lines=9))

        relay.wait_for(lambda: relay.events)
        posted = relay.events[0]
        assert set(posted["sealed"]) == {d.device_id for d in paired}
        for device in paired:
            sent = json.loads(
                linkcrypto.open_sealed(
                    linked.device_keys(device).h2d,
                    posted["sealed"][device.device_id],
                    f"{AAD_EVT}{HOST_ID}:e0e0e0e0:1",
                )
            )
            kept = sent["data"]["text"]
            assert 0 < len(kept) < len(text)
            assert text.endswith(kept)
            assert kept.split("\n")[0] in lines  # whole lines only
            assert sent["data"]["lines"] == 9  # the rest of the event as it was
        run.finish(relay)

    def test_a_box_drawing_pane_keeps_most_of_what_fits(self, tmp_path, relay, phone):
        # A real Claude Code pane is full of box-drawing lines and glyphs, and
        # each costs six bytes once sealed (json.dumps escapes it): a cut sized
        # in characters kept nothing of a pane whose best tail is ~18k.
        paired = (
            phone,
            new_device(TABLET_ID, "Tablet DECOY"),
            new_device(OTHER_ID, "Other DECOY"),
        )
        host = FakeHost()
        linked, run = _online(tmp_path, relay, paired, host)
        lines = [
            "─" * 100 if i % 2 else f"│ line {i:05d} " + "x" * 80 for i in range(400)
        ]
        text = "\n".join(lines)
        host.feed.push(_event(1, "session.pane", session="a", text=text, lines=9))

        relay.wait_for(lambda: relay.events, timeout=15)
        posted = relay.events[0]
        item_size = len(json.dumps(posted, separators=(",", ":")).encode()) + 1
        assert len('{"events":[]}') + item_size <= link.EVENTS_MAX_BYTES
        for device in paired:
            sent = json.loads(
                linkcrypto.open_sealed(
                    linked.device_keys(device).h2d,
                    posted["sealed"][device.device_id],
                    f"{AAD_EVT}{HOST_ID}:e0e0e0e0:1",
                )
            )
            kept = sent["data"]["text"]
            assert text.endswith(kept)
            assert kept.split("\n")[0] in lines  # whole lines only
            assert len(kept) >= 15_000  # the best whole-line tail is ~18k
        run.finish(relay)

    def test_a_cut_through_the_last_line_keeps_that_line(self, tmp_path, relay, phone):
        # One long line ending in a newline: whole lines only would send an
        # empty pane. The partial last line goes out instead.
        host = FakeHost()
        linked, run = _online(tmp_path, relay, (phone,), host)
        text = "y" * 300_000 + "\n"
        host.feed.push(_event(1, "session.pane", session="a", text=text))
        relay.wait_for(lambda: relay.events, timeout=15)
        sent = json.loads(
            linkcrypto.open_sealed(
                linked.device_keys(phone).h2d,
                relay.events[0]["sealed"][phone.device_id],
                f"{AAD_EVT}{HOST_ID}:e0e0e0e0:1",
            )
        )
        kept = sent["data"]["text"]
        assert len(kept) > 100_000
        assert text.endswith(kept)
        run.finish(relay)

    def test_the_pane_cut_is_logged_once_a_minute(self, tmp_path, relay, phone, caplog):
        # A busy pane is cut on every capture: one line a minute in link.log.
        host = FakeHost()
        with caplog.at_level("INFO", logger="magent.link"):
            _linked, run = _online(tmp_path, relay, (phone,), host)
            text = "\n".join(f"line {n:07d} DECOY" for n in range(15_000))
            for n in (1, 2, 3):
                host.feed.push(_event(n, "session.pane", session="a", text=text))
            relay.wait_for(lambda: len(relay.events) == 3)
            run.finish(relay)
        cuts = [r for r in caplog.records if "pane was cut" in r.getMessage()]
        assert len(cuts) == 1

    def test_a_pane_that_fits_is_sent_whole(self, tmp_path, relay, phone):
        host = FakeHost()
        linked, run = _online(tmp_path, relay, (phone,), host)
        text = "x" * 150_000
        host.feed.push(_event(1, "session.pane", session="a", text=text))
        relay.wait_for(lambda: relay.events)
        sent = json.loads(
            linkcrypto.open_sealed(
                linked.device_keys(phone).h2d,
                relay.events[0]["sealed"][phone.device_id],
                f"{AAD_EVT}{HOST_ID}:e0e0e0e0:1",
            )
        )
        assert sent["data"]["text"] == text
        run.finish(relay)

    @pytest.mark.parametrize("bad", ["id", "long-id", "type", "emoji-type"])
    def test_an_event_the_relay_would_refuse_is_left_out(
        self, tmp_path, relay, phone, bad
    ):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        broken = _event(1, session="a")
        if bad == "id":
            broken["id"] = 7
        elif bad == "long-id":
            broken["id"] = "e" * 65
        elif bad == "emoji-type":
            broken["type"] = EMOJI * 33  # 66 code units: over 64
        else:
            broken["type"] = "t" * 65
        host.feed.push(broken, _event(2, session="b"))

        relay.wait_for(lambda: relay.events)
        time.sleep(0.3)
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:2"]
        run.finish(relay)

    @pytest.mark.parametrize("kind", ["reset", "hello"])
    def test_a_reset_puts_a_fresh_snapshot_and_is_not_forwarded(
        self, tmp_path, relay, phone, kind
    ):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        before = len(relay.snapshots)
        host.feed.push(_event(9, kind, reason="evicted"), _event(10, session="a"))

        relay.wait_for(lambda: relay.events)
        if kind == "reset":
            relay.wait_for(lambda: len(relay.snapshots) == before + 1)
        else:
            time.sleep(0.3)
            assert len(relay.snapshots) == before
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:10"]
        run.finish(relay)

    def test_the_feed_is_closed_when_the_runner_stops(self, tmp_path, relay, phone):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        run.finish(relay)
        assert host.feed.closed
        assert stray_forwarders(timeout=0) == []

    def test_the_forwarder_stops_when_the_runner_is_revoked(
        self, tmp_path, relay, phone
    ):
        # A revoked runner ends on its own, without its stop event: the
        # forwarder must end with it all the same.
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        relay.revoke()
        assert run.wait_done() == "revoked"
        assert host.feed.closed
        assert stray_forwarders(timeout=0) == []


class TestRateLimit:
    def test_a_429_is_waited_out_through_the_runner_s_sleep(
        self, tmp_path, relay, phone
    ):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        relay.rate_limit_next_calls(1, retry_after=7)
        host.feed.push(_event(1, session="a"))

        relay.wait_for(lambda: relay.events)
        assert 7.0 in run.sleeps  # asked of the runner's sleep, never time.sleep
        assert _event_posts(relay) == 2
        run.finish(relay)

    def test_a_retry_after_past_the_cap_holds_posts_until_it_passes(
        self, monkeypatch, tmp_path, relay, phone
    ):
        # The client does not wait out a Retry-After over its cap: it raises.
        # That batch is dropped, and so is every batch until the relay's
        # Retry-After, cut to RETRY_AFTER_MAX_S, has passed.
        monkeypatch.setattr(link, "RETRY_AFTER_MAX_S", 1.0)
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        relay.rate_limit_next_calls(1, retry_after=99999)
        host.feed.push(_event(1, session="a"))
        relay.wait_for(lambda: _event_posts(relay) == 1)
        refused = time.monotonic()
        host.feed.push(_event(2, session="b"))
        time.sleep(0.5)
        assert _event_posts(relay) == 1  # held: e2 never left the host
        time.sleep(max(0.0, refused + 1.1 - time.monotonic()))
        host.feed.push(_event(3, session="c"))
        relay.wait_for(lambda: relay.events)
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:3"]
        assert 99999 not in run.sleeps
        run.finish(relay)


class TestResilience:
    def test_a_failing_subscribe_backs_off_and_retries(
        self, monkeypatch, tmp_path, relay, phone, caplog
    ):
        monkeypatch.setattr(link, "EVENT_RETRY_BASE_S", 0.01)
        fails = [3]

        class Flaky(FakeHost):
            def subscribe(self):
                if fails[0] > 0:
                    fails[0] -= 1
                    raise RuntimeError("DECOY subscribe failed")
                return super().subscribe()

        host = Flaky()
        with caplog.at_level("INFO", logger="magent.link"):
            _linked, run = _online(tmp_path, relay, (phone,), host)
            host.feed.push(_event(1, session="a"))
            relay.wait_for(lambda: relay.events)
        assert fails == [0]
        assert run.thread.is_alive()
        tracebacks = [r for r in caplog.records if r.exc_info]
        assert len(tracebacks) == 1  # the first of a streak; the rest one line
        run.finish(relay)

    def test_a_feed_that_breaks_is_closed_and_read_again(
        self, monkeypatch, tmp_path, relay, phone
    ):
        monkeypatch.setattr(link, "EVENT_RETRY_BASE_S", 0.01)
        feeds: list[FakeFeed] = []

        class Breaking(FakeFeed):
            def next_batch(self, timeout):
                raise OSError("DECOY feed broke")

        class Host(FakeHost):
            def subscribe(self):
                feed = Breaking() if not feeds else self.feed
                feeds.append(feed)
                return feed

        host = Host()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        relay.wait_for(lambda: len(feeds) >= 2)
        host.feed.push(_event(1, session="a"))
        relay.wait_for(lambda: relay.events)
        assert feeds[0].closed
        run.finish(relay)

    def test_a_crash_while_sending_never_ends_the_thread(self, tmp_path, relay, phone):
        # A reset's snapshot read failing on the daemon side: logged, the
        # batch dropped, the next one forwarded from the same feed.
        crash = [False]
        subscribes = [0]

        class Host(FakeHost):
            def subscribe(self):
                subscribes[0] += 1
                return super().subscribe()

            def snapshot(self):
                if crash[0]:
                    crash[0] = False
                    raise RuntimeError("DECOY snapshot crash")
                return super().snapshot()

        host = Host()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        crash[0] = True
        host.feed.push(_event(1, "reset", reason="evicted"))
        relay.wait_for(lambda: not crash[0])
        host.feed.push(_event(2, session="b"))
        relay.wait_for(lambda: relay.events)
        assert run.thread.is_alive()
        assert subscribes == [1]
        assert not host.feed.closed
        run.finish(relay)


def test_nothing_is_posted_while_the_link_is_not_online(tmp_path, relay, phone):
    linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
    runner = LinkRunner(
        FakeHost(), link_path=linked.link_path, state_path=linked.state_path
    )
    forwarder = EventForwarder(
        runner, RelayClient(relay.url, token=relay.host_token), FakeHost().subscribe
    )
    forwarder.send([_event(1, session="a"), _event(2, "reset", reason="epoch")])
    assert relay.requests == []


def test_stop_ends_a_forwarder_blocked_on_a_quiet_feed(relay):
    # A feed that blocks its whole timeout (as the daemon's does): stop still
    # returns once that read does, and the feed is closed.
    class Quiet(FakeFeed):
        def next_batch(self, timeout):
            try:
                return [self._queue.get(timeout=timeout)]
            except queue.Empty:
                return []

    feed = Quiet()
    runner = LinkRunner(FakeHost())
    forwarder = EventForwarder(
        runner, RelayClient(relay.url, token=relay.host_token), lambda: feed
    )
    forwarder.start()
    began = time.monotonic()
    forwarder.stop()
    assert time.monotonic() - began < link.EVENT_IDLE_WAIT_S + 0.5
    assert feed.closed
    assert not [t for t in threading.enumerate() if t.name == link.EVENT_THREAD]


class _Stub:
    """A runner as the forwarder sees it: online on demand, every seal for
    one fake device, and the snapshot re-PUTs counted."""

    host_id = HOST_ID

    def __init__(self, online: bool, *, backing_off: bool = False) -> None:
        self.is_online = online
        self.is_backing_off = backing_off
        self.snapshots = 0

    def online(self) -> bool:
        return self.is_online

    def backing_off(self) -> bool:
        return self.is_backing_off

    def remote_control_changed(self) -> bool:
        return False

    def watch_link(self) -> None:
        pass

    def seal_for_devices(self, plain: bytes, aad: str) -> dict[str, str]:
        return {}

    def put_snapshot(self, client: RelayClient) -> None:
        self.snapshots += 1


class _Feed(FakeFeed):
    """A FakeFeed that counts its reads."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def next_batch(self, timeout):
        self.reads += 1
        return super().next_batch(timeout)


class TestResync:
    """A dropped batch is repaired by a snapshot re-PUT as soon as the link
    can post, not at the next reconnect (a healthy stream can last hours)."""

    def test_after_a_rate_limit_hold_the_snapshot_is_put_again(
        self, monkeypatch, tmp_path, relay, phone
    ):
        monkeypatch.setattr(link, "RETRY_AFTER_MAX_S", 0.5)
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        before = len(relay.snapshots)
        relay.rate_limit_next_calls(1, retry_after=99999)
        host.feed.push(_event(1, session="a"))

        relay.wait_for(lambda: len(relay.snapshots) == before + 1)
        assert relay.events == []  # e1 was lost; the snapshot repaired it
        run.finish(relay)

    def test_a_failed_post_is_followed_by_a_snapshot(self, tmp_path, relay, phone):
        fails = [1]

        class Failing(RelayClient):
            def call(self, method, path, body=None, **kwargs):
                if path == "/v1/host/events" and fails[0] > 0:
                    fails[0] -= 1
                    raise link.RelayError(500, "internal", "DECOY relay fault")
                return super().call(method, path, body, **kwargs)

        def factory(url, token, *, sleep):
            return Failing(url, token=token, sleep=sleep)

        linked = write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)
        host = FakeHost()
        run = Running(host, linked, client_factory=factory)
        relay.wait_for(run.runner.online)
        before = len(relay.snapshots)
        host.feed.push(_event(1, session="a"))

        relay.wait_for(lambda: len(relay.snapshots) == before + 1)
        assert fails == [0]
        assert relay.events == []
        run.finish(relay)

    def test_a_batch_dropped_while_connecting_is_repaired_once_online(self, relay):
        runner = _Stub(online=False)
        feed = _Feed()
        forwarder = EventForwarder(
            runner, RelayClient(relay.url, token=relay.host_token), lambda: feed
        )
        forwarder.send([_event(1, session="a")])  # the snapshot did not see it
        assert relay.requests == []
        runner.is_online = True
        forwarder.start()
        try:
            relay.wait_for(lambda: runner.snapshots == 1)
            reads = feed.reads
            relay.wait_for(lambda: feed.reads >= reads + 3)
            assert runner.snapshots == 1  # repaired once, not on every read
        finally:
            forwarder.stop()
        assert relay.requests == []

    def test_a_remote_control_switch_reaches_the_relay_without_a_reconnect(
        self, tmp_path, relay, phone
    ):
        # The relay mirrors remoteControl from the snapshot; on a healthy
        # stream nothing else would re-PUT it for hours.
        host = FakeHost(rc=False)
        _linked, run = _online(tmp_path, relay, (phone,), host)
        relay.wait_for(lambda: relay.snapshots)
        before = len(relay.snapshots)
        assert relay.snapshots[-1]["remoteControl"] is False
        host.rc = True
        relay.wait_for(
            lambda: (
                len(relay.snapshots) == before + 1
                and relay.snapshots[-1]["remoteControl"] is True
            ),
            timeout=15,
        )
        time.sleep(0.3)
        assert len(relay.snapshots) == before + 1  # once, not on every read
        run.finish(relay)


class TestOfflineFeed:
    """While the link backs off (minutes to hours with the relay down), the
    daemon's feed is not read: each read holds a bus subscription, which
    keeps serve sampling every pane only for the batch to be dropped."""

    def test_the_feed_is_not_read_while_the_link_backs_off(self, relay):
        runner = _Stub(online=False, backing_off=True)
        feed = _Feed()
        forwarder = EventForwarder(
            runner, RelayClient(relay.url, token=relay.host_token), lambda: feed
        )
        forwarder.start()
        try:
            time.sleep(1.0)
            assert feed.reads <= 1
            # Connected again: the feed is read, and the snapshot repaired
            # once, since the reads it skipped may have held a drop.
            runner.is_backing_off = False
            runner.is_online = True
            relay.wait_for(lambda: feed.reads >= 3)
            relay.wait_for(lambda: runner.snapshots == 1)
        finally:
            forwarder.stop()
        assert relay.requests == []

    def test_a_link_still_connecting_reads_the_feed(self, relay):
        # Before the first connect is through, the feed is read as before:
        # its cursor must start before the snapshot the connect puts.
        runner = _Stub(online=False)
        feed = _Feed()
        forwarder = EventForwarder(
            runner, RelayClient(relay.url, token=relay.host_token), lambda: feed
        )
        forwarder.start()
        try:
            relay.wait_for(lambda: feed.reads >= 3)
        finally:
            forwarder.stop()


class TestDeviceSet:
    """Every event is sealed for the devices trusted at that moment."""

    def test_a_device_revoked_mid_run_gets_no_more_events(self, tmp_path, relay, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        host = FakeHost()
        linked, run = _online(tmp_path, relay, (phone, tablet), host)
        host.feed.push(_event(1, session="a"))
        relay.wait_for(lambda: relay.events)
        assert set(relay.events[0]["sealed"]) == {phone.device_id, TABLET_ID}

        relay.set_devices(
            [phone.wire(), tablet.wire(revoked_at="2026-10-08T13:00:00.000Z")]
        )
        relay.wait_for(lambda: linked.device_ids() == [phone.device_id])
        relay.wait_for(lambda: set(relay.snapshots[-1]["sealed"]) == {phone.device_id})
        host.feed.push(_event(2, session="a"))
        relay.wait_for(lambda: len(relay.events) == 2)
        assert set(relay.events[1]["sealed"]) == {phone.device_id}
        run.finish(relay)

    def test_a_device_paired_mid_run_gets_the_next_event(self, tmp_path, relay, phone):
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        host = FakeHost()
        linked, run = _online(tmp_path, relay, (phone,), host)
        host.feed.push(_event(1, session="a"))
        relay.wait_for(lambda: relay.events)
        assert set(relay.events[0]["sealed"]) == {phone.device_id}

        save_linked(
            linked,
            host_token=relay.host_token,
            devices=(phone.trusted(), tablet.trusted()),
        )
        relay.set_devices([phone.wire(), tablet.wire()])
        relay.wait_for(
            lambda: set(relay.snapshots[-1]["sealed"]) == {phone.device_id, TABLET_ID}
        )
        host.feed.push(_event(2, session="a"))
        relay.wait_for(lambda: len(relay.events) == 2)
        assert set(relay.events[1]["sealed"]) == {phone.device_id, TABLET_ID}
        run.finish(relay)


def _item(n: int, pad: int) -> dict[str, object]:
    return {
        "daemonEventId": f"e0e0e0e0:{n}",
        "type": "session.pane",
        "meta": {},
        "sealed": {"d": "A" * pad},
    }


def _body(chunk: list[dict[str, object]]) -> int:
    """The bytes RelayClient.call sends for one chunk."""
    return len(json.dumps({"events": chunk}, separators=(",", ":")).encode("utf-8"))


class TestBatchEdges:
    @pytest.mark.parametrize(
        ("over", "chunks"), [(-1, 1), (1, 2)], ids=["1-byte-under", "1-byte-over"]
    )
    def test_the_split_is_exact_at_the_256_kib_cap(self, relay, over, chunks):
        forwarder = EventForwarder(_Stub(online=True), RelayClient(relay.url), FakeFeed)
        first = _item(1, 100_000)
        overhead = _body([first, _item(2, 0)])  # both items, the second empty
        pair = [first, _item(2, link.EVENTS_MAX_BYTES - overhead + over)]
        assert _body(pair) == link.EVENTS_MAX_BYTES + over

        out = forwarder._chunks(pair)
        assert len(out) == chunks
        assert all(_body(chunk) <= link.EVENTS_MAX_BYTES for chunk in out)

    def test_a_reset_anywhere_in_a_batch_puts_the_snapshot(
        self, tmp_path, relay, phone
    ):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        before = len(relay.snapshots)
        host.feed.push(_event(1, session="a"), _event(2, "reset", reason="evicted"))

        relay.wait_for(lambda: len(relay.snapshots) == before + 1)
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:1"]
        run.finish(relay)

    def test_a_batch_read_while_stopping_is_never_sent(self, relay, monkeypatch):
        # A batching window no loaded machine can outrun: the stop below
        # always lands inside it.
        monkeypatch.setattr(link, "EVENT_BATCH_S", 60.0)
        feed = _Feed()
        forwarder = EventForwarder(
            _Stub(online=True),
            RelayClient(relay.url, token=relay.host_token),
            lambda: feed,
        )
        feed.push(_event(1, session="a"))
        forwarder.start()
        relay.wait_for(lambda: feed.reads >= 1)  # read, inside its batching window
        forwarder.stop()
        assert relay.requests == []

    def test_something_that_is_not_an_event_is_left_out(self, tmp_path, relay, phone):
        host = FakeHost()
        _linked, run = _online(tmp_path, relay, (phone,), host)
        host.feed.push(7, _event(2, session="b"))

        relay.wait_for(lambda: relay.events)
        assert [e["daemonEventId"] for e in relay.events] == ["e0e0e0e0:2"]
        run.finish(relay)


class TestMetaEdges:
    def test_a_long_name_is_cut_to_200(self):
        meta = event_meta(_event(1, "session.added", session="a", name="n" * 250))
        assert meta["name"] == "n" * 200

    def test_an_unknown_live_is_false_as_in_the_snapshot(self):
        meta = event_meta(_event(1, session="a", live=None))
        assert meta == {"session": "a", "live": False}


class TestPaneTail:
    """link.pane_tail: the cut itself, with an exact cost to cover."""

    @pytest.mark.parametrize(
        ("text", "need", "tail"),
        [
            # The cut lands on a line start: that line is kept whole.
            ("aaaa\nbbbb\ncccc\n", 5, "bbbb\ncccc\n"),
            # ...mid-line: the partial line goes.
            ("aaaa\nbbbb\ncccc\n", 3, "bbbb\ncccc\n"),
            ("aaaa\nbbbb\ncccc\n", 7, "cccc\n"),
            # Its only newline is its last character: the partial line stays.
            ("yyyy\n", 2, "yy\n"),
            # A newline costs two once escaped, so "aa\n" covers 4.
            ("aa\nbb\ncc", 4, "bb\ncc"),
            ("", 3, ""),
        ],
    )
    def test_whole_lines_only(self, text, need, tail):
        assert link.pane_tail(text, need) == tail

    def test_a_surrogate_pair_is_never_split(self):
        text = EMOJI * 4 + "\n" + "z" * 4
        # An emoji costs 12 once escaped: a need of 13 drops two of them,
        # and the partial first line then goes.
        assert link.pane_tail(text, 13) == "z" * 4
        assert link.pane_tail(EMOJI * 4, 13) == EMOJI * 2
