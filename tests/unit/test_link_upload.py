"""Relay upload pulls: download, decrypt, stage, report -- and every way an
upload is refused, retried or replayed without a second paste."""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from magent import link, linkcrypto, linkfiles
from magent.link import (
    LinkRunner,
    RelayClient,
    RelayError,
    UploadPuller,
    safe_name,
    upload_state,
)
from magent.linkcrypto import AAD_UKEY
from tests._fake_relay import FakeRelay
from tests.unit._link_fakes import (
    OTHER_ID,
    STRANGER_ID,
    FakeHost,
    Running,
    make_upload,
    new_device,
    stop_all,
    stray_forwarders,
    up_id,
    write_link,
)


@pytest.fixture(autouse=True)
def _no_runner_outlives_its_test():
    """Torn down last (autouse), after the relay: a test that failed before
    finish() leaves no runner, forwarder or puller thread behind."""
    yield
    assert stop_all() == [], "a link runner did not stop"
    assert stray_forwarders() == [], "a forwarder or upload puller did not stop"


@pytest.fixture
def relay():
    with FakeRelay() as fake:
        yield fake


@pytest.fixture
def phone():
    return new_device()


@pytest.fixture
def linked(tmp_path, relay, phone):
    return write_link(tmp_path, relay.url, (phone,), host_token=relay.host_token)


def _states(relay: FakeRelay, upload_id: str) -> list[str]:
    with relay._cond:
        results = list(relay.results)
    return [
        str(r["state"])
        for r in results
        if r.get("type") == "upload" and r.get("uploadId") == upload_id
    ]


def _pulls(upload_id: str) -> list[Path]:
    """What is left of the upload's pull folders under .incoming."""
    return sorted(linkfiles.INCOMING_DIR.glob(f"{upload_id}*"))


@pytest.fixture
def tokens(monkeypatch):
    """Name each pull's folder in turn: <uploadId>.t1, <uploadId>.t2, ..."""
    names = (f"t{n}" for n in itertools.count(1))
    monkeypatch.setattr(link, "_pull_token", lambda: next(names))


def _blob_gets(relay: FakeRelay, upload_id: str) -> int:
    with relay._cond:
        requests = list(relay.requests)
    return sum(
        1
        for method, path, _ in requests
        if method == "GET" and path.startswith(f"/blob/{upload_id}")
    )


class _Downloads(RelayClient):
    """Scripts ``get_bytes``: fail the next ``fails[0]`` downloads, or hold
    every download until ``gate`` is set (``entered`` says one started)."""

    def __init__(self, url, *, token, sleep, fails, gate, entered):
        super().__init__(url, token=token, sleep=sleep)
        self._fails, self._gate, self._entered = fails, gate, entered

    def get_bytes(self, url, sink, *, max_bytes, deadline=link.DOWNLOAD_DEADLINE_S):
        self._entered.set()
        self._gate.wait(5)
        if self._fails[0] > 0:
            self._fails[0] -= 1
            raise RelayError(0, "network", "scripted drop")
        return super().get_bytes(url, sink, max_bytes=max_bytes, deadline=deadline)


def _downloads(fails=0, gate=None, entered=None):
    fails_box = [fails]
    if gate is None:
        gate = threading.Event()
        gate.set()
    entered = entered or threading.Event()

    def factory(url, token, *, sleep):
        return _Downloads(
            url, token=token, sleep=sleep, fails=fails_box, gate=gate, entered=entered
        )

    return factory


class TestHelpers:
    @pytest.mark.parametrize(
        ("name", "safe"),
        [
            ("photo.png", "photo.png"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\evil<1>.png", "evil1.png"),
            # A dotfile keeps its name behind "_", as the daemon's own saved
            # names do; it can still never collide with ".blob".
            ("  .hidden. ", "_.hidden"),
            (".env", "_.env"),
            (". . .", "upload.bin"),
            ("", "upload.bin"),
            (None, "upload.bin"),
            ("CON", "_CON"),
            ("nul.txt", "_nul.txt"),
            ("CON .txt", "_CON .txt"),
            ("COM¹.log", "_COM¹.log"),
            ("lpt³", "_lpt³"),
            ("CONSOLE.txt", "CONSOLE.txt"),
            ("a\x00b\x1f.png", "ab.png"),
            # 150 UTF-8 bytes at most: the stem is cut, the suffix kept.
            ("x" * 300 + ".png", "x" * 146 + ".png"),
            ("é" * 150 + ".png", "é" * 73 + ".png"),
            # A "suffix" past 20 bytes is no extension: cut as the stem.
            ("a." + "b" * 200, "a." + "b" * 148),
            # A cut that lands on a space or a dot drops it.
            ("x" * 145 + " " + "y" * 10 + ".png", "x" * 145 + ".png"),
        ],
    )
    def test_safe_name(self, name, safe):
        assert safe_name(name) == safe
        assert len(safe.encode("utf-8")) <= 150

    @pytest.mark.parametrize(
        ("envelope", "inject", "state"),
        [
            ({"ok": True, "data": {"paste": "injected"}}, True, "injected"),
            ({"ok": True, "data": {"paste": "pending"}}, True, "inject_pending"),
            ({"ok": True, "data": {"paste": "saved"}}, True, "inject_failed"),
            ({"ok": True, "data": {"paste": "saved"}}, False, "pulled"),
            (
                {"ok": False, "error": {"code": "not_found", "message": "x"}},
                True,
                "inject_failed",
            ),
        ],
    )
    def test_upload_state(self, envelope, inject, state):
        assert upload_state(envelope, inject) == state


class TestStates:
    """Upload results move forward only: pulled, then inject_pending, then
    injected or inject_failed (the relay's ranks)."""

    @pytest.fixture
    def puller(self, tmp_path):
        runner = LinkRunner(
            FakeHost(),
            link_path=tmp_path / "link.json",
            state_path=tmp_path / "link-state.json",
        )
        return UploadPuller(runner, RelayClient("http://127.0.0.1:9"), FakeHost())

    def test_a_state_moves_forward(self, puller):
        uid = up_id(90)
        assert puller._record(uid, "pulled")
        assert puller._record(uid, "inject_pending")
        assert puller._record(uid, "injected")
        assert puller._states[uid] == "injected"

    @pytest.mark.parametrize(
        ("first", "then"),
        [
            ("injected", "pulled"),
            ("injected", "inject_pending"),
            ("inject_failed", "injected"),
            ("inject_pending", "pulled"),
            ("pulled", "pulled"),
        ],
    )
    def test_a_state_never_moves_back_or_repeats(self, puller, first, then):
        uid = up_id(91)
        assert puller._record(uid, first)
        assert not puller._record(uid, then)
        assert puller._states[uid] == first


class TestPull:
    def test_a_pulled_file_is_staged_pasted_and_cleaned_up(self, relay, phone, linked):
        uid = up_id(1)
        host = FakeHost(max_bytes=10 * 1024 * 1024)
        run = Running(host, linked)
        data = os.urandom(3 * 1024 * 1024 + 5)  # four chunks, the last one short
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=data)
        )

        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        assert host.staged == [
            (phone.device_id, "magent-alpha", "photo.png", data, True)
        ]
        assert _pulls(uid) == []
        assert uid in linked.state().seen_ids
        run.finish(relay)

    @pytest.mark.parametrize(
        ("paste", "inject", "states"),
        [
            ("pending", True, ["pulled", "inject_pending"]),
            ("saved", True, ["pulled", "inject_failed"]),
            ("saved", False, ["pulled"]),
        ],
    )
    def test_the_paste_outcome_is_reported(
        self, relay, phone, linked, paste, inject, states
    ):
        uid = up_id(2)
        host = FakeHost(stage_reply={"ok": True, "data": {"paste": paste}})
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG", inject=inject)
        )

        relay.wait_for(lambda: host.staged and _states(relay, uid) == states)
        assert host.staged[0][4] is inject
        run.finish(relay)
        assert _states(relay, uid) == states  # "pulled" is never sent twice

    def test_a_staging_crash_is_reported_failed(self, relay, phone, linked):
        uid = up_id(3)

        class _Crashing(FakeHost):
            def stage_upload(self, device_id, session, file_path, inject):
                raise RuntimeError("DECOY stage crash")

        host = _Crashing()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "inject_failed"])
        assert _pulls(uid) == []
        run.finish(relay)

    def test_the_name_is_reduced_to_a_safe_base_name(self, relay, phone, linked):
        uid = up_id(4)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(
                relay,
                linked,
                phone,
                upload_id=uid,
                data=b"x",
                name="..\\..\\evil<1>.png",
            )
        )
        relay.wait_for(lambda: host.staged)
        assert host.staged[0][2] == "evil1.png"
        run.finish(relay)

    def test_long_polling_delivers_uploads_too(self, relay, phone, linked):
        uid = up_id(5)
        relay.fail_next_connects(3)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )

        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        # Every poll re-lists it until "pulled": skipped while in flight.
        assert len(host.staged) == 1
        assert any(p.startswith("/v1/host/commands") for _, p, _ in relay.requests)
        run.finish(relay)

    def test_a_pull_never_stalls_the_command_stream(self, relay, phone, linked):
        from tests.unit._link_fakes import cmd_id, make_command

        gate, entered = threading.Event(), threading.Event()
        host = FakeHost()
        run = Running(
            host, linked, client_factory=_downloads(gate=gate, entered=entered)
        )
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=up_id(6), data=b"PNG")
        )
        assert entered.wait(5)
        relay.queue_command(make_command(linked, phone, cid=cmd_id(60), seq=1))
        relay.wait_for(lambda: 1 in relay.acks)  # answered while the pull is held
        gate.set()
        relay.wait_for(lambda: _states(relay, up_id(6)) == ["pulled", "injected"])
        run.finish(relay)


class TestRefusals:
    def test_a_tampered_object_is_refused_whole(self, relay, phone, linked):
        uid = up_id(10)
        host = FakeHost(max_bytes=10 * 1024 * 1024)
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(
                relay,
                linked,
                phone,
                upload_id=uid,
                data=os.urandom(2_000_000),
                tamper=True,
            )
        )
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert host.staged == []
        assert _pulls(uid) == []
        assert uid not in linked.state().seen_ids
        run.finish(relay)

    def test_an_upload_declared_over_the_cap_is_never_downloaded(
        self, relay, phone, linked
    ):
        uid = up_id(11)
        host = FakeHost(max_bytes=1000)
        run = Running(host, linked)
        ready = make_upload(
            relay, linked, phone, upload_id=uid, data=b"x", size=1000 + 65536 + 1
        )
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert _blob_gets(relay, uid) == 0
        run.finish(relay)

    def test_an_upload_declared_at_the_cap_is_taken(self, relay, phone, linked):
        uid = up_id(12)
        host = FakeHost(max_bytes=1000)
        run = Running(host, linked)
        ready = make_upload(
            relay, linked, phone, upload_id=uid, data=b"x", size=1000 + 65536
        )
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        run.finish(relay)

    def test_an_object_bigger_than_the_cap_is_cut_off(self, relay, phone, linked):
        uid = up_id(13)
        host = FakeHost(max_bytes=1000)
        run = Running(host, linked)
        ready = make_upload(
            relay, linked, phone, upload_id=uid, data=os.urandom(100_000), size=10
        )
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert host.staged == []
        assert uid not in linked.state().seen_ids
        run.finish(relay)

    def test_an_upload_from_an_untrusted_device_is_refused(self, relay, phone, linked):
        uid = up_id(14)
        stranger = new_device(STRANGER_ID)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, stranger, upload_id=uid, data=b"x")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert host.staged == []
        assert _blob_gets(relay, uid) == 0
        assert uid not in linked.state().seen_ids
        run.finish(relay)

    def test_a_key_wrapped_for_another_upload_is_refused(self, relay, phone, linked):
        uid = up_id(15)
        host = FakeHost()
        run = Running(host, linked)
        other = make_upload(relay, linked, phone, upload_id=up_id(16), data=b"x")
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        ready["wrappedKey"] = other["wrappedKey"]
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert _blob_gets(relay, uid) == 0
        assert uid not in linked.state().seen_ids
        run.finish(relay)

    def test_a_name_sealed_for_another_upload_is_refused(self, relay, phone, linked):
        uid = up_id(17)
        host = FakeHost()
        run = Running(host, linked)
        other = make_upload(relay, linked, phone, upload_id=up_id(18), data=b"x")
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        ready["sealedMeta"] = other["sealedMeta"]
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert _blob_gets(relay, uid) == 0
        assert uid not in linked.state().seen_ids
        run.finish(relay)

    def test_a_refusal_never_blocks_the_genuine_upload(self, relay, phone, linked):
        # A relay that sends junk under a real upload's id first gets it
        # refused, but the id stays out of the ring and the result cache:
        # the genuine upload.ready that follows is still pulled and pasted.
        uid = up_id(19)
        host = FakeHost()
        run = Running(host, linked)
        genuine = make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        other = make_upload(relay, linked, phone, upload_id=up_id(20), data=b"x")
        relay.queue_upload_ready({**genuine, "wrappedKey": other["wrappedKey"]})
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        assert uid not in run.runner._uploads._states

        run.runner._uploads.offer(genuine)
        relay.wait_for(lambda: host.staged)
        assert host.staged[0][3] == b"PNG"
        run.finish(relay)

    @pytest.mark.parametrize(
        "bad",
        [
            "../keep",
            "..",
            "",
            None,
            7,
            "UP-" + "0" * 33,
            up_id(21) + "/..",
            up_id(21).upper(),  # one folder on Windows: lowercase only
        ],
    )
    def test_an_upload_id_that_is_not_a_uuid_is_ignored(
        self, relay, phone, linked, bad
    ):
        # The id names a folder that is removed after the pull: anything but
        # a UUID could aim that removal outside the incoming folder.
        beside = linkfiles.INCOMING_DIR.parent / "keep" / "keep.txt"
        inside = linkfiles.INCOMING_DIR / up_id(99) / "keep.txt"
        for keep in (beside, inside):
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_text("DECOY keep", encoding="utf-8")
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=up_id(21), data=b"x")
        relay.wait_for(lambda: run.runner._uploads is not None)
        run.runner._uploads.offer({**ready, "uploadId": bad})
        relay.wait_for(lambda: not run.runner._uploads._inflight)
        run.finish(relay)
        assert relay.results == []
        assert host.staged == []
        assert beside.exists() and inside.exists()

    @pytest.mark.parametrize(
        "url", ["ftp://storage.example.com/o?sig=DECOY_s", "http://127.0.0.1:99999/o"]
    )
    def test_a_download_url_that_is_not_allowed_is_reported_failed(
        self, relay, phone, linked, url
    ):
        # Refused by policy, not lost on the way: retrying it on every connect
        # would never succeed, so the app is told.
        uid = up_id(22)
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        relay.queue_upload_ready({**ready, "getUrl": url})
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        run.finish(relay)
        assert host.staged == []
        assert uid not in linked.state().seen_ids

    def test_a_file_that_cannot_be_written_here_is_refused_and_a_resend_works(
        self, relay, phone, linked, tokens
    ):
        uid = up_id(23)
        # A folder where the first pull's decrypted file must go: opening it
        # fails. The resend pulls into a folder of its own.
        (linkfiles.INCOMING_DIR / f"{uid}.t1" / "photo.png").mkdir(parents=True)
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"], timeout=15)
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight, timeout=15)
        assert uid not in linked.state().seen_ids
        assert _pulls(uid) == []

        run.runner._uploads.offer(ready)  # never remembered: a resend is pulled
        relay.wait_for(lambda: host.staged, timeout=15)
        assert host.staged[0][3] == b"PNG"
        run.finish(relay)

    def test_a_blob_that_cannot_be_written_is_refused(
        self, relay, phone, linked, tokens
    ):
        # The download side of "could not be written here" (a full disk
        # mid-download): refused, not a crash the next connect repeats.
        uid = up_id(41)
        (linkfiles.INCOMING_DIR / f"{uid}.t1" / ".blob").mkdir(parents=True)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        run.finish(relay)
        assert host.staged == []
        assert uid not in linked.state().seen_ids

    def test_a_blob_that_cannot_be_deleted_still_stages_the_file(
        self, relay, phone, linked, monkeypatch
    ):
        # A scanner holding the decrypted-from .blob a moment must not refuse
        # a file that decrypted fine: the folder is removed anyway.
        real_unlink = Path.unlink

        def held(self, *args, **kwargs):
            if self.name == ".blob":
                raise PermissionError("DECOY held by a scanner")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", held)
        uid = up_id(42)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        run.finish(relay)
        assert host.staged[0][3] == b"PNG"

    def test_a_content_key_of_the_wrong_length_is_refused(self, relay, phone, linked):
        uid = up_id(24)
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        short = linkcrypto.seal(
            linked.device_keys(phone).d2h, os.urandom(16), AAD_UKEY + uid
        )
        relay.queue_upload_ready({**ready, "wrappedKey": short})
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert _blob_gets(relay, uid) == 0
        run.finish(relay)

    def test_a_negative_size_is_refused(self, relay, phone, linked):
        uid = up_id(25)
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        relay.queue_upload_ready({**ready, "sizeBytes": -1})
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        assert _blob_gets(relay, uid) == 0
        run.finish(relay)

    def test_only_inject_true_asks_for_a_paste(self, relay, phone, linked):
        uid = up_id(26)
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"x")
        relay.queue_upload_ready({**ready, "inject": "false"})
        relay.wait_for(lambda: host.staged)
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        run.finish(relay)
        assert host.staged[0][4] is False
        assert _states(relay, uid) == ["pulled"]

    def test_an_unsaved_ring_reports_failed_and_never_pastes(
        self, relay, phone, linked, monkeypatch
    ):
        # A restart would not know it was pasted, so it is not pasted.
        uid = up_id(27)
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(lambda: run.runner._uploads is not None)
        monkeypatch.setattr(run.runner, "_save", lambda: False)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["inject_failed"])
        run.finish(relay)
        assert host.staged == []

    def test_a_stop_mid_download_abandons_it(self, relay, phone, linked):
        # Stopping never waits out a long download, and what was cut short is
        # neither remembered nor reported: the relay resends it next run.
        uid = up_id(28)
        gate, entered = threading.Event(), threading.Event()
        host = FakeHost()
        run = Running(
            host, linked, client_factory=_downloads(gate=gate, entered=entered)
        )
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        assert entered.wait(5)
        puller = run.runner._uploads
        puller._stop.set()  # what stop() does first
        gate.set()
        relay.wait_for(lambda: uid not in puller._inflight)
        run.finish(relay)
        assert host.staged == []
        assert _states(relay, uid) == []
        assert uid not in linked.state().seen_ids
        assert _pulls(uid) == []


class TestReplays:
    def test_a_replayed_upload_is_reported_again_not_pasted_again(
        self, relay, phone, linked
    ):
        uid = up_id(30)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])

        with relay._cond:
            relay.results.clear()  # the relay lost the results: it replays upload.ready
        relay.wait_for(lambda: (relay.drop_streams() or True) and _states(relay, uid))
        assert _states(relay, uid)[0] == "injected"
        assert len(host.staged) == 1
        assert _blob_gets(relay, uid) == 1
        run.finish(relay)

    def test_an_upload_seen_before_a_restart_is_reported_pulled_never_fetched(
        self, relay, phone, linked
    ):
        uid = up_id(31)
        linkfiles.save_state(linkfiles.LinkState(seen_ids=(uid,)), linked.state_path)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["pulled"])
        run.finish(relay)
        assert host.staged == []
        assert _blob_gets(relay, uid) == 0

    def test_each_pull_has_a_folder_of_its_own(self, relay, phone, linked, monkeypatch):
        # A puller from an earlier run of the link can still be finishing the
        # same upload: it must never truncate or remove what this one stages.
        folders: list[Path] = []
        real = UploadPuller._take

        def spy(self, upload_id, ready, folder):
            folders.append(folder)
            return real(self, upload_id, ready, folder)

        monkeypatch.setattr(UploadPuller, "_take", spy)
        uid = up_id(43)
        host = FakeHost()
        run = Running(host, linked, client_factory=_downloads(fails=1))
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(
            lambda: (
                (relay.drop_streams() or True)
                and _states(relay, uid) == ["pulled", "injected"]
            )
        )
        run.finish(relay)
        assert len(folders) == 2
        assert folders[0] != folders[1]
        for folder in folders:
            assert folder.parent == linkfiles.INCOMING_DIR
            assert folder.name.startswith(f"{uid}.")
        assert _pulls(uid) == []

    def test_a_stop_after_the_decrypt_abandons_the_upload(
        self, relay, phone, linked, monkeypatch
    ):
        # Stopped between the decrypt and the ring: neither remembered,
        # staged nor reported, so the next run of the link pulls it whole.
        real = UploadPuller._fetch

        def then_stop(self, ready, folder):
            target = real(self, ready, folder)
            self._stop.set()
            return target

        monkeypatch.setattr(UploadPuller, "_fetch", then_stop)
        uid = up_id(44)
        host = FakeHost()
        run = Running(host, linked)
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(lambda: _blob_gets(relay, uid) == 1)
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        run.finish(relay)
        assert host.staged == []
        assert _states(relay, uid) == []
        assert uid not in linked.state().seen_ids
        assert _pulls(uid) == []

    def test_a_failed_download_is_retried_on_the_next_connect(
        self, relay, phone, linked
    ):
        uid = up_id(32)
        host = FakeHost()
        run = Running(host, linked, client_factory=_downloads(fails=1))
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )

        relay.wait_for(
            lambda: (
                (relay.drop_streams() or True)
                and _states(relay, uid) == ["pulled", "injected"]
            )
        )
        assert len(host.staged) == 1
        assert _blob_gets(relay, uid) == 1  # the scripted drop never reached it
        run.finish(relay)

    def test_a_failed_download_is_not_remembered(self, relay, phone, linked):
        uid = up_id(33)
        gate = threading.Event()
        host = FakeHost()
        run = Running(host, linked, client_factory=_downloads(fails=1, gate=gate))
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        relay.wait_for(
            lambda: (
                run.runner._uploads is not None and uid in run.runner._uploads._inflight
            )
        )
        gate.set()
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        assert uid not in linked.state().seen_ids
        assert _states(relay, uid) == []
        run.finish(relay)

    def test_a_duplicate_while_in_flight_is_skipped(self, relay, phone, linked):
        uid = up_id(34)
        gate, entered = threading.Event(), threading.Event()
        host = FakeHost()
        run = Running(
            host, linked, client_factory=_downloads(gate=gate, entered=entered)
        )
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        relay.queue_upload_ready(ready)
        assert entered.wait(5)

        run.runner._uploads.offer(ready)
        run.runner._uploads.offer(ready)
        # The first delivery is out of the queue and held at the gate: the
        # two offers queued nothing.
        assert run.runner._uploads._queue.qsize() == 0
        assert _states(relay, uid) == []  # nothing re-reported for a pull in flight
        gate.set()

        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        assert len(host.staged) == 1
        assert _blob_gets(relay, uid) == 1
        run.finish(relay)

    def test_an_id_remembered_meanwhile_is_reported_not_pasted(
        self, relay, phone, linked
    ):
        # The seen check that decides is the ring's add itself, made after
        # the object decrypted: an id another delivery remembered while this
        # one downloaded is reported, never pasted a second time.
        uid = up_id(36)
        gate, entered = threading.Event(), threading.Event()
        host = FakeHost()
        run = Running(
            host, linked, client_factory=_downloads(gate=gate, entered=entered)
        )
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        )
        assert entered.wait(5)
        assert run.runner.remember(uid) is True  # the other delivery's add
        gate.set()
        relay.wait_for(lambda: _states(relay, uid) == ["pulled"])
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        run.finish(relay)
        assert host.staged == []
        assert _pulls(uid) == []

    def test_the_presigned_url_never_reaches_the_log(
        self, relay, phone, linked, caplog
    ):
        uid = up_id(35)
        caplog.set_level(logging.DEBUG, logger="magent")
        host = FakeHost()
        run = Running(host, linked)
        ready = make_upload(relay, linked, phone, upload_id=uid, data=b"PNG")
        # A URL the relay answers 404: the download fails and is logged.
        ready["getUrl"] = f"{relay.url}/blob/missing?X-Sig=DECOY_presigned"
        relay.queue_upload_ready(ready)
        relay.wait_for(lambda: _blob_gets(relay, "missing") >= 1)
        relay.wait_for(lambda: uid not in run.runner._uploads._inflight)
        run.finish(relay)
        assert any(uid in r.getMessage() for r in caplog.records)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "DECOY_presigned" not in text
        assert "/blob/" not in text


class TestThread:
    def test_stop_wakes_an_idle_puller_at_once(self, tmp_path, monkeypatch):
        # An idle puller blocks on its queue; stop() must not wait that out.
        monkeypatch.setattr(link, "UPLOAD_QUEUE_WAIT_S", 5.0)
        runner = LinkRunner(
            FakeHost(),
            link_path=tmp_path / "link.json",
            state_path=tmp_path / "link-state.json",
        )
        puller = UploadPuller(runner, RelayClient("http://127.0.0.1:9"), FakeHost())
        puller.start()
        time.sleep(0.1)  # into its queue wait
        started = time.monotonic()
        puller.stop()
        assert time.monotonic() - started < 2.0
        assert stray_forwarders(timeout=0) == []

    def test_the_puller_runs_on_its_own_named_thread(self, relay, linked):
        run = Running(FakeHost(), linked)
        relay.wait_for(lambda: run.runner._uploads is not None)
        names = {t.name for t in threading.enumerate()}
        assert link.UPLOAD_THREAD in names
        assert link.UPLOAD_THREAD == "magent-relay-uploads"  # the public name
        run.finish(relay)
        assert stray_forwarders(timeout=0) == []

    def test_the_puller_stops_even_when_the_forwarder_stop_fails(
        self, relay, linked, monkeypatch
    ):
        real_stop = link.EventForwarder.stop

        def failing_stop(self):
            real_stop(self)
            raise RuntimeError("DECOY forwarder stop failure")

        monkeypatch.setattr(link.EventForwarder, "stop", failing_stop)
        runner = LinkRunner(
            FakeHost(), link_path=linked.link_path, state_path=linked.state_path
        )
        stop = threading.Event()
        client = runner._linked_client(stop.wait)
        assert client is not None
        stop.set()  # the loop ends at once; the finally does the rest
        with pytest.raises(RuntimeError, match="DECOY forwarder stop failure"):
            runner._run_linked(client, stop, stop.wait)
        assert stray_forwarders(timeout=0) == []


class TestStrangers:
    def test_a_device_paired_mid_run_can_upload(self, relay, phone, linked):
        tablet = new_device(OTHER_ID, "Tablet DECOY")
        from tests.unit._link_fakes import save_linked

        uid = up_id(40)
        host = FakeHost()
        run = Running(host, linked)
        relay.wait_for(run.runner.online)
        save_linked(
            linked,
            host_token=relay.host_token,
            devices=(phone.trusted(), tablet.trusted()),
        )
        relay.set_devices([phone.wire(), tablet.wire()])  # re-reads link.json
        relay.wait_for(lambda: tablet.device_id in run.runner._peers)
        relay.queue_upload_ready(
            make_upload(relay, linked, tablet, upload_id=uid, data=b"T")
        )
        relay.wait_for(lambda: _states(relay, uid) == ["pulled", "injected"])
        assert host.staged[0][0] == tablet.device_id
        run.finish(relay)
