"""The link's state words (contract C8) as status, doctor and /api/v1/meta
show them; who on this machine runs the link (the link lock and the record
its holder writes); the Sentry scrubber; and the rule that the host token
never reaches a log."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading

import pytest

from magent import api, env, link, linkfiles, lockfile, sentry
from magent.cli import doctor
from magent.link import UPGRADE_HINT, describe_link
from tests._fake_relay import FakeRelay
from tests.unit._link_fakes import (
    FakeHost,
    Running,
    cmd_id,
    make_command,
    new_device,
    stop_all,
    write_link,
)

TOKEN = "mlh_DECOY_status_token_000000000000000000000000"
UPLOAD_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a99"


@pytest.fixture
def link_on(monkeypatch):
    monkeypatch.setenv("MAGENT_LINK", "1")
    monkeypatch.setattr(env, "_cached_env", None)


@pytest.fixture
def linked(tmp_path):
    return write_link(tmp_path, "http://127.0.0.1:9", (new_device(),), host_token=TOKEN)


@pytest.fixture
def at_home(linked, monkeypatch):
    """``linked`` where every reader looks by default."""
    monkeypatch.setattr(linkfiles, "LINK_PATH", linked.link_path)
    monkeypatch.setattr(linkfiles, "LINK_STATE_PATH", linked.state_path)
    return linked


def _state(linked, **fields: object) -> None:
    linkfiles.save_state(linkfiles.LinkState(**fields), linked.state_path)


def _describe(linked, *, serving: bool | str = True, now: float = 1000.0):
    return describe_link(
        serving=lambda: serving,
        now=now,
        link_path=linked.link_path,
        state_path=linked.state_path,
    )


@contextlib.contextmanager
def _serve_runs_the_link(**record: object):
    with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
        linkfiles.save_serve({"pid": 4242, **record})
        try:
            yield
        finally:
            linkfiles.clear_serve()


class TestDescribe:
    def test_disabled_wins(self, linked):
        assert _describe(linked).as_json() == {"state": "disabled", "retry_in_s": None}

    def test_unlinked(self, link_on, tmp_path):
        status = describe_link(serving=lambda: True, link_path=tmp_path / "none.json")
        assert (status.state, status.detail) == ("unlinked", "unlinked")

    def test_an_unreadable_link_file_says_how_to_repair(self, link_on, tmp_path):
        bad = tmp_path / "link.json"
        bad.write_text("{}", encoding="utf-8")
        status = describe_link(serving=lambda: True, link_path=bad)
        assert status.state == "unlinked"
        assert "magent unlink" in status.detail

    def test_a_host_token_no_request_could_carry_says_how_to_repair(
        self, link_on, tmp_path
    ):
        bad = write_link(
            tmp_path, "http://127.0.0.1:9", (new_device(),), host_token="mlh_DECOY x"
        )
        status = describe_link(serving=lambda: True, link_path=bad.link_path)
        assert status.state == "unlinked"
        assert "`magent unlink`" in status.detail

    @pytest.mark.parametrize(
        ("fields", "serving", "state", "retry", "detail"),
        [
            ({"state": "connecting"}, True, "connecting", None, "connecting"),
            ({"state": "online"}, True, "online", None, "online"),
            (
                {"state": "offline", "retry_at": 1012.2},
                True,
                "offline",
                13,
                "offline (retry in 13s)",
            ),
            (
                {"state": "offline", "retry_at": 990.0},
                True,
                "offline",
                0,
                "offline (retry in 0s)",
            ),
            (
                {"state": "online"},
                False,
                "offline",
                None,
                "offline (magent serve is not running)",
            ),
            (
                {"state": "connecting"},
                "the serve is up but runs no link",
                "offline",
                None,
                "offline (the serve is up but runs no link)",
            ),
            (
                {"state": "revoked"},
                True,
                "revoked",
                None,
                "revoked (run `magent unlink`, then `magent link`)",
            ),
            (
                {"state": "too_old"},
                True,
                "offline",
                None,
                "offline (the relay needs a newer magent)",
            ),
            ({"state": "unlinked"}, True, "connecting", None, "connecting"),
        ],
    )
    def test_the_state_words(
        self, link_on, linked, fields, serving, state, retry, detail
    ):
        _state(linked, **fields)
        status = _describe(linked, serving=serving)
        assert (status.state, status.retry_in_s, status.detail) == (
            state,
            retry,
            detail,
        )

    def test_a_state_of_another_pairing_reads_as_fresh(self, link_on, linked):
        _state(linked, state="revoked", host_id="hst_DECOY_old")
        assert _describe(linked).state == "connecting"
        _state(linked, state="revoked", host_id="hst_fake_1")
        assert _describe(linked).state == "revoked"

    def test_too_old_carries_the_upgrade_hint(self, link_on, linked):
        _state(linked, state="too_old")
        assert _describe(linked).hint == UPGRADE_HINT

    def test_an_offline_link_that_never_connected_says_where_to_look(
        self, link_on, linked
    ):
        _state(linked, state="offline", retry_at=1012.0)
        assert "not connected since it was paired" in (_describe(linked).hint or "")
        _state(linked, state="offline", retry_at=1012.0, last_connected=900.0)
        assert _describe(linked).hint is None

    def test_the_serve_is_only_asked_when_it_matters(self, link_on, tmp_path):
        asked: list[int] = []
        describe_link(
            serving=lambda: bool(asked.append(1)), link_path=tmp_path / "none.json"
        )
        assert asked == []


class TestWhoRunsTheLink:
    def test_no_lock_file_means_no_serve_and_creates_nothing(self):
        assert link.link_serve() == link.LinkServe(held=False)
        assert not lockfile.lock_path(link.LINK_LOCK).exists()

    def test_a_free_lock_means_no_serve(self):
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            pass
        linkfiles.save_serve({"pid": 1, "port": 8033, "config": "stale"})
        assert link.link_serve() == link.LinkServe(held=False)

    def test_a_held_lock_names_its_serve(self):
        with _serve_runs_the_link(port=8044, config="C:/cfg/magent.config.json"):
            assert link.link_serve() == link.LinkServe(
                held=True, pid=4242, port=8044, config="C:/cfg/magent.config.json"
            )

    def test_a_held_lock_without_its_record_is_still_held(self):
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            assert link.link_serve() == link.LinkServe(held=True)

    def test_a_lock_that_cannot_be_probed_is_unknown(self, monkeypatch):
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            pass

        def refused(*_a, **_k):
            raise PermissionError("DECOY")

        monkeypatch.setattr(lockfile, "persistent_lock", refused)
        assert link.link_serve() == link.LinkServe(held=None)


class _Stopped:
    def __init__(self, seen: list[object]) -> None:
        self._seen = seen

    def run(self, stop: threading.Event) -> str:
        self._seen.append(linkfiles.load_serve())
        self._seen.append(sorted(p.name for p in linkfiles.INCOMING_DIR.iterdir()))
        return "stopped"


class TestTheServeThatRunsTheLink:
    def test_it_writes_its_record_while_it_holds_the_lock(self, link_on):
        seen: list[object] = []
        linkfiles.INCOMING_DIR.mkdir(parents=True)
        link.supervise(
            object,
            threading.Event(),
            runner_factory=lambda _h: _Stopped(seen),
            serve={"port": 8044, "config": "C:/cfg/magent.config.json"},
        )
        assert seen[0] == {
            "pid": os.getpid(),
            "port": 8044,
            "config": "C:/cfg/magent.config.json",
        }
        assert linkfiles.load_serve() is None  # gone with the lock

    def test_it_clears_pulls_a_dead_serve_left_before_its_runner_starts(self, link_on):
        incoming = linkfiles.INCOMING_DIR
        stale = incoming / f"{UPLOAD_ID}.0a1b2c3d"
        (stale / "inner").mkdir(parents=True)
        (stale / "inner" / "photo.png").write_bytes(b"DECOY")
        keep = [
            incoming / UPLOAD_ID,  # not this layout: never ours to judge
            incoming / f"{UPLOAD_ID}.XYZ",
            incoming / "notes",
        ]
        for folder in keep:
            folder.mkdir()
        (incoming / f"{UPLOAD_ID}.0a1b2c3e").write_bytes(b"a file, not a pull")
        seen: list[object] = []
        link.supervise(
            object,
            threading.Event(),
            runner_factory=lambda _h: _Stopped(seen),
        )
        assert seen[1] == sorted([*(f.name for f in keep), f"{UPLOAD_ID}.0a1b2c3e"])

    def test_a_record_it_cannot_write_leaves_no_stale_one(self, link_on, monkeypatch):
        linkfiles.save_serve({"pid": 1, "port": 8099, "config": "C:/dead/DECOY.json"})

        def cannot_write(_record):
            raise OSError("DECOY: read-only")

        monkeypatch.setattr(linkfiles, "save_serve", cannot_write)
        seen: list[object] = []
        linkfiles.INCOMING_DIR.mkdir(parents=True)
        link.supervise(
            object,
            threading.Event(),
            runner_factory=lambda _h: _Stopped(seen),
            serve={"port": 8044, "config": "C:/cfg/magent.config.json"},
        )
        assert seen[0] is None  # held, no record: not the dead serve's facts

    def test_a_serve_that_stands_by_neither_writes_nor_sweeps(self, link_on):
        stale = linkfiles.INCOMING_DIR / f"{UPLOAD_ID}.0a1b2c3d"
        stale.mkdir(parents=True)
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            link.supervise(
                object,
                threading.Event(),
                runner_factory=lambda _h: _Stopped([]),
                sleep=lambda _s: True,
                serve={"port": 8044},
            )
        assert stale.exists()
        assert linkfiles.load_serve() is None


class TestSurfaces:
    def test_doctor_warns_on_a_revoked_link(self, link_on, at_home):
        _state(at_home, state="revoked")
        assert doctor._check_link(None) == (
            "warn",
            "revoked (run `magent unlink`, then `magent link`)",
        )

    def test_doctor_is_ok_while_disabled(self):
        assert doctor._check_link(None) == ("ok", "disabled (MAGENT_LINK=0)")

    def test_doctor_gives_the_upgrade_hint(self, link_on, at_home):
        _state(at_home, state="too_old")
        status, detail = doctor._check_link(None)
        assert status == "warn"
        assert UPGRADE_HINT in detail

    def test_doctor_warns_when_the_link_serve_reads_another_config(
        self, link_on, at_home, tmp_path
    ):
        _state(at_home, state="online")
        mine = tmp_path / "mine.json"
        theirs = tmp_path / "theirs.json"
        with _serve_runs_the_link(port=8033, config=str(theirs)):
            status, detail = doctor._check_link(None, mine)
        assert status == "warn"
        assert detail.startswith("online")
        assert str(theirs) in detail
        assert str(mine) in detail

    def test_doctor_is_ok_when_they_read_the_same_file(
        self, link_on, at_home, tmp_path
    ):
        _state(at_home, state="online")
        mine = tmp_path / "mine.json"
        mine.write_text("{}", encoding="utf-8")
        with _serve_runs_the_link(port=8033, config=str(mine)):
            assert doctor._check_link(None, mine) == ("ok", "online")

    def test_doctor_warns_when_the_link_serve_found_no_config(
        self, link_on, at_home, tmp_path
    ):
        _state(at_home, state="online")
        with _serve_runs_the_link(port=8033, config=None):
            status, detail = doctor._check_link(None, tmp_path / "mine.json")
        assert status == "warn"
        assert "found no config file" in detail
        assert "remote control is off there" in detail

    def test_a_held_lock_without_a_record_warns_about_nothing(
        self, link_on, at_home, tmp_path
    ):
        _state(at_home, state="online")
        with lockfile.persistent_lock(link.LINK_LOCK, wait_s=0.0):
            assert doctor._check_link(None, tmp_path / "mine.json") == (
                "ok",
                "online",
            )

    def test_doctor_says_when_a_serve_is_up_but_runs_no_link(
        self, link_on, at_home, monkeypatch
    ):
        monkeypatch.setattr("magent.cli.status._upload_state", lambda _port: "on")
        _state(at_home, state="connecting")
        status, detail = doctor._check_link(None)
        assert status == "warn"
        assert detail.startswith("offline (magent serve is up but is not running")

    def test_status_prints_a_link_line(self, runner, tmp_config, monkeypatch):
        from magent import cli

        monkeypatch.setattr("magent.cli.status._health_check", lambda port: False)
        monkeypatch.setattr("magent.cli.status._probe_port", lambda port: False)
        cfgpath = tmp_config({"projects": []})
        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])
        assert "  Link             disabled (MAGENT_LINK=0)" in result.output

    def test_status_names_a_config_mismatch_and_never_the_relay_userinfo(
        self, runner, tmp_config, monkeypatch, link_on, tmp_path
    ):
        from magent import cli

        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        write_link(
            tmp_path,
            "http://DECOY:pw_DECOY@127.0.0.1:9",
            (new_device(),),
            host_token=TOKEN,
        )
        monkeypatch.setattr(linkfiles, "LINK_PATH", tmp_path / "link.json")
        monkeypatch.setattr(linkfiles, "LINK_STATE_PATH", tmp_path / "link-state.json")
        linkfiles.save_state(linkfiles.LinkState(state="online"))
        cfgpath = tmp_config({"projects": []})
        theirs = tmp_path / "theirs.json"
        with _serve_runs_the_link(port=8033, config=str(theirs)):
            result = runner.invoke(cli.main, ["--config", cfgpath, "status"])
        assert "  Link             online" in result.output
        assert f"the magent serve running the link reads {theirs}" in result.output
        assert "pw_DECOY" not in result.output

    def test_status_json_carries_the_link(self, link_on, at_home):
        from magent.cli import status as status_mod

        _state(at_home, state="offline", retry_at=10**12)
        report = status_mod._link_report(lambda: False, port=8033, config_file=None)
        assert report.status.as_json() == {"state": "offline", "retry_in_s": None}

    def test_meta_reports_the_state_word(self, link_on, at_home):
        _state(at_home, state="online")
        with _serve_runs_the_link(port=8033):
            assert api._link_state() == "online"
        # No serve holds the link: this serve is not running it either.
        assert api._link_state() == "offline"

    def test_meta_falls_back_to_the_link_thread_when_the_lock_cannot_be_read(
        self, link_on, at_home, monkeypatch
    ):
        _state(at_home, state="online")
        monkeypatch.setattr(link, "link_serve", lambda: link.LinkServe(held=None))
        assert api._link_state() == "offline"  # no link thread in this process
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, name=link.LINK_THREAD, daemon=True)
        thread.start()
        try:
            assert api._link_state() == "online"
        finally:
            stop.set()
            thread.join(5)

    def test_a_lock_that_cannot_be_probed_is_a_doctor_warning(
        self, link_on, at_home, monkeypatch
    ):
        _state(at_home, state="online")
        monkeypatch.setattr(link, "link_serve", lambda: link.LinkServe(held=None))
        monkeypatch.setattr("magent.cli.status._upload_state", lambda _port: "on")
        status, detail = doctor._check_link(None)
        assert status == "warn"
        assert detail.startswith("online")
        assert "cannot be probed" in detail


class TestSentryScrub:
    def test_link_secrets_never_reach_sentry(self):
        event = {
            "message": f"relay said no for Bearer {TOKEN}",
            "request": {
                "headers": {"Authorization": f"Bearer {TOKEN}", "Accept": "*/*"}
            },
            "extra": {
                "host_token": TOKEN,
                "hostToken": TOKEN,
                "host_kex_priv": "DECOY-key",
                "hostSignPriv": "DECOY-sign",
                "poll_token": "mlp_DECOY_x",
                "pollToken": "mlp_DECOY_y",
            },
            "breadcrumbs": {"values": [{"message": f"PUT /v1/host/snapshot {TOKEN}"}]},
            "exception": {"values": [{"value": f"401 unauthorized ({TOKEN})"}]},
        }
        scrubbed = sentry.scrub_event(event, {})
        text = json.dumps(scrubbed)
        for secret in (TOKEN, "DECOY-key", "DECOY-sign", "mlp_DECOY_x", "mlp_DECOY_y"):
            assert secret not in text
        assert scrubbed["request"]["headers"]["Accept"] == "*/*"
        assert scrubbed["message"] == "relay said no for Bearer [redacted]"

    def test_any_bearer_value_and_the_qr_secret_are_cut_from_text(self):
        event = {
            "logentry": {
                "message": "auth Bearer DECOYopaque123 failed",
                "params": [
                    "https://app.example/pair#c=ABCDEFGH&k=DECOYfp&s=DECOYsecret"
                ],
            },
            "tags": ("bearer DECOYlower",),
        }
        text = json.dumps(sentry.scrub_event(event, {}))
        assert "DECOYopaque123" not in text
        assert "DECOYsecret" not in text
        assert "DECOYlower" not in text
        assert "c=ABCDEFGH&k=DECOYfp&s=[redacted]" in text

    def test_init_installs_the_scrubber(self, monkeypatch):
        from tests.unit.test_sentry import _install_fake_sentry_sdk

        fake_sdk = _install_fake_sentry_sdk(monkeypatch)
        sentry.init_sentry("https://DECOY@sentry.example/1")
        assert fake_sdk.init_calls[0]["before_send"] is sentry.scrub_event


def test_the_host_token_never_reaches_a_log(tmp_path, caplog):
    phone = new_device()
    try:
        with FakeRelay() as relay, caplog.at_level(logging.DEBUG, logger="magent"):
            linked = write_link(
                tmp_path, relay.url, (phone,), host_token=relay.host_token
            )
            relay.set_devices([phone.wire()])
            run = Running(FakeHost(), linked)
            relay.queue_command(make_command(linked, phone, cid=cmd_id(1), seq=1))
            relay.wait_for(lambda: relay.results, timeout=15)
            relay.revoke()
            relay.drop_streams()
            assert run.wait_done(15) == "revoked"
    finally:
        stop_all()
    assert caplog.records
    assert relay.host_token not in caplog.text
    assert relay.host_token not in "\n".join(str(r.args) for r in caplog.records)
