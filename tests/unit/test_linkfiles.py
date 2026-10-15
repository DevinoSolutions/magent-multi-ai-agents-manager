"""linkfiles: ~/.magent/link.json (secret) and link-state.json (cursor)."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from magent import linkfiles
from magent.linkfiles import LinkFile, LinkState, TrustedDevice

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")

DEVICE = TrustedDevice(
    device_id="0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a8b",
    name="Pixel",
    kex_pub="a" * 43,
    sign_pub="b" * 43,
    paired_at="2026-10-08T22:00:00+00:00",
)
LINK = LinkFile(
    relay_url="https://relay.example.com",
    host_id="hst_1",
    host_token="mlh_DECOY_" + "x" * 33,
    host_kex_priv="c" * 43,
    host_sign_priv="d" * 43,
    devices=(DEVICE,),
)
SECRETS = (LINK.host_token, LINK.host_kex_priv, LINK.host_sign_priv)


def _no_secret_in(exc: BaseException) -> None:
    for secret in SECRETS:
        assert secret not in str(exc)


def test_the_paths_live_under_the_redirected_home():
    magent = Path.home() / ".magent"
    assert magent / "link.json" == linkfiles.LINK_PATH
    assert magent / "link-state.json" == linkfiles.LINK_STATE_PATH
    assert magent / "uploads" / ".incoming" == linkfiles.INCOMING_DIR


class TestLinkFile:
    def test_the_repr_hides_the_token_and_both_keys(self):
        text = repr(LINK)
        assert "hst_1" in text
        for secret in SECRETS:
            assert secret not in text

    def test_missing_is_none(self, tmp_path):
        assert linkfiles.load_link(tmp_path / "link.json") is None

    def test_round_trip(self, tmp_path):
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        assert linkfiles.load_link(path) == LINK

    def test_the_default_path_is_used(self):
        linkfiles.save_link(LINK)
        assert linkfiles.LINK_PATH.is_file()
        assert linkfiles.load_link() == LINK

    def test_the_file_has_the_contract_shape(self, tmp_path):
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert set(data) == {
            "version",
            "relay_url",
            "host_id",
            "host_token",
            "host_kex_priv",
            "host_sign_priv",
            "devices",
        }
        assert data["devices"][0] == {
            "device_id": DEVICE.device_id,
            "name": "Pixel",
            "kex_pub": DEVICE.kex_pub,
            "sign_pub": DEVICE.sign_pub,
            "paired_at": DEVICE.paired_at,
        }

    @POSIX
    def test_it_is_owner_only(self, tmp_path):
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600

    @POSIX
    def test_a_readable_file_is_refused(self, tmp_path):
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        path.chmod(0o644)
        with pytest.raises(linkfiles.LinkFileError, match="644"):
            linkfiles.load_link(path)

    def test_a_non_private_file_is_refused(self, tmp_path):
        # No POSIX skip: write_atomic leaves Windows' inherited DACL, which
        # check_private refuses, so this pins the read-side check everywhere.
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        body = path.read_bytes()
        path.unlink()
        from magent import fsutil

        fsutil.write_atomic(path, body)
        path.chmod(0o644)  # POSIX: readable whatever the umask
        with pytest.raises(linkfiles.LinkFileError) as caught:
            linkfiles.load_link(path)
        _no_secret_in(caught.value)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d.update(version=2), id="version-2"),
            pytest.param(lambda d: d.update(version=True), id="version-true"),
            pytest.param(lambda d: d.update(version=1.0), id="version-float"),
            pytest.param(lambda d: d.pop("version"), id="no-version"),
            pytest.param(lambda d: d.pop("host_token"), id="no-token"),
            pytest.param(lambda d: d.update(devices="nope"), id="devices-str"),
            pytest.param(lambda d: d.update(devices=["nope"]), id="device-str"),
            pytest.param(lambda d: d["devices"][0].pop("sign_pub"), id="no-pub"),
            pytest.param(lambda d: d.update(host_id=7), id="host-id-int"),
            pytest.param(lambda d: d.update(extra="x"), id="unknown-key"),
            pytest.param(
                lambda d: d["devices"][0].update(extra="x"), id="unknown-device-key"
            ),
        ],
    )
    def test_a_wrong_shape_is_refused(self, tmp_path, mutate):
        path = tmp_path / "link.json"
        linkfiles.save_link(LINK, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        from magent import fsutil

        fsutil.write_private(path, json.dumps(data).encode())
        with pytest.raises(linkfiles.LinkFileError) as caught:
            linkfiles.load_link(path)
        _no_secret_in(caught.value)

    def test_not_json_is_refused(self, tmp_path):
        from magent import fsutil

        path = tmp_path / "link.json"
        body = (
            '{"host_token": "%s", "host_kex_priv": "%s", "host_sign_priv": "%s", nope'
        )
        fsutil.write_private(path, (body % SECRETS).encode())
        with pytest.raises(
            linkfiles.LinkFileError, match="not a magent link file"
        ) as caught:
            linkfiles.load_link(path)
        _no_secret_in(caught.value)
        # The decode error holds the file's text: it must not ride along.
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


class TestState:
    def test_missing_is_the_fresh_state(self, tmp_path):
        assert linkfiles.load_state(tmp_path / "s.json") == LinkState()

    def test_round_trip(self, tmp_path):
        path = tmp_path / "s.json"
        state = LinkState(
            cursor=42,
            seen_ids=("a", "b"),
            last_connected=1.5,
            state="online",
            retry_at=None,
        )
        linkfiles.save_state(state, path)
        assert linkfiles.load_state(path) == state

    def test_seen_ids_keep_only_the_newest_1024(self, tmp_path):
        path = tmp_path / "s.json"
        ids = tuple(f"id{i}" for i in range(1500))
        linkfiles.save_state(LinkState(seen_ids=ids), path)
        loaded = linkfiles.load_state(path)
        assert len(loaded.seen_ids) == 1024
        assert loaded.seen_ids[0] == "id476" and loaded.seen_ids[-1] == "id1499"

    def test_the_save_side_keeps_only_the_newest_1024(self, tmp_path):
        path = tmp_path / "s.json"
        ids = tuple(f"id{i}" for i in range(1500))
        linkfiles.save_state(LinkState(seen_ids=ids), path)
        saved = json.loads(path.read_text(encoding="utf-8"))["seen_ids"]
        assert len(saved) == 1024
        assert saved[0] == "id476" and saved[-1] == "id1499"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("[1, 2", id="corrupt"),
            pytest.param("[1, 2]", id="not-an-object"),
            pytest.param('{"cursor": 5}', id="no-version"),
            pytest.param('{"version": 2, "cursor": 5}', id="version-2"),
            pytest.param('{"version": true, "cursor": 5}', id="version-true"),
            pytest.param('{"version": 1.0, "cursor": 5}', id="version-float"),
        ],
    )
    def test_an_unusable_state_is_the_fresh_state_and_warns(
        self, tmp_path, caplog, body
    ):
        path = tmp_path / "s.json"
        path.write_text(body, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="magent.link"):
            assert linkfiles.load_state(path) == LinkState()
        assert [r.getMessage() for r in caplog.records] == [
            f"link: {path} is unreadable; starting from a fresh state"
        ]

    def test_a_read_that_fails_for_a_moment_is_retried(self, tmp_path, monkeypatch):
        # On Windows a read racing the writer's atomic replace is refused for
        # a moment; that must not look like a fresh state (a lost seen ring).
        path = tmp_path / "s.json"
        state = LinkState(cursor=7, seen_ids=("a",), state="online")
        linkfiles.save_state(state, path)
        real = linkfiles._read_json
        fails = [2]

        def flaky(p):
            if fails[0] > 0:
                fails[0] -= 1
                raise PermissionError("DECOY sharing violation")
            return real(p)

        monkeypatch.setattr(linkfiles, "_read_json", flaky)
        monkeypatch.setattr(linkfiles, "READ_RETRY_S", 0.0)
        assert linkfiles.load_state(path) == state
        assert fails == [0]

    def test_a_read_that_keeps_failing_is_the_fresh_state(
        self, tmp_path, monkeypatch, caplog
    ):
        path = tmp_path / "s.json"
        linkfiles.save_state(LinkState(cursor=7), path)
        calls = []

        def broken(p):
            calls.append(p)
            raise PermissionError("DECOY sharing violation")

        monkeypatch.setattr(linkfiles, "_read_json", broken)
        monkeypatch.setattr(linkfiles, "READ_RETRY_S", 0.0)
        with caplog.at_level(logging.WARNING, logger="magent.link"):
            assert linkfiles.load_state(path) == LinkState()
        assert len(calls) == linkfiles.READ_ATTEMPTS
        assert len(caplog.records) == 1

    def test_wrong_field_types_fall_back_one_by_one(self, tmp_path):
        path = tmp_path / "s.json"
        body = {
            "version": 1,
            "cursor": True,
            "seen_ids": "abc",
            "last_connected": True,
            "state": 3,
            "retry_at": "soon",
        }
        path.write_text(json.dumps(body), encoding="utf-8")
        assert linkfiles.load_state(path) == LinkState()

    def test_good_fields_survive_bad_neighbours(self, tmp_path):
        path = tmp_path / "s.json"
        body = {
            "version": 1,
            "cursor": "7",
            "seen_ids": ["a", 3, None, "b"],
            "last_connected": 2,
            "state": "online",
            "retry_at": [],
        }
        path.write_text(json.dumps(body), encoding="utf-8")
        assert linkfiles.load_state(path) == LinkState(
            cursor=0, seen_ids=("a", "b"), last_connected=2.0, state="online"
        )


def test_delete_removes_both_files(tmp_path):
    link_path, state_path = tmp_path / "link.json", tmp_path / "s.json"
    linkfiles.save_link(LINK, link_path)
    linkfiles.save_state(LinkState(cursor=1), state_path)
    linkfiles.delete_link(link_path, state_path)
    assert not link_path.exists() and not state_path.exists()
    linkfiles.delete_link(link_path, state_path)  # already gone: no error
