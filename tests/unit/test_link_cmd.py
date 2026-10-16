"""`magent link` / `magent unlink` against the in-process fake relay."""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timezone

import pytest

from magent import cli, config, linkcrypto, linkfiles, linkpair, lockfile
from magent.cli import link_cmd
from magent.config import load_config
from magent.link import LINK_LOCK, UPGRADE_HINT
from tests.unit._link_fakes import OTHER_ID, PHONE_ID, TABLET_ID, new_device

SECRET = b"\x07" * 16  # the QR secret, pinned so the test can play the app
ANSWER_HELP = (
    "Answer y only if the app shows this same number. "
    "If you scanned the QR code, the app shows no number: answer N."
)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


@pytest.fixture
def relay(fake_relay):
    """The conftest fake relay, its code good for the relay's 10 minutes."""
    fake_relay.expires_at = _iso(time.time() + 600)
    return fake_relay


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "magent.config.json"
    path.write_text(
        json.dumps({"version": config.SCHEMA_VERSION, "projects": []}),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _pinned_secret(monkeypatch):
    monkeypatch.setattr(linkpair.secrets, "token_bytes", lambda n: SECRET[:n])


def _claimed(device, *, qr: bool) -> dict[str, object]:
    raw: dict[str, object] = {
        "deviceId": device.device_id,
        "name": device.name,
        "kexPub": linkcrypto.b64u_encode(device.keys.kex_pub),
        "signPub": linkcrypto.b64u_encode(device.keys.sign_pub),
    }
    if qr:
        raw["mac"] = linkcrypto.pair_mac(
            SECRET, device.keys.kex_pub, device.keys.sign_pub
        )
    return raw


def _link(runner, cfg, *args: str, input: str | None = None):  # noqa: A002  # reason: click's CliRunner keyword
    return runner.invoke(cli.main, ["--config", str(cfg), "link", *args], input=input)


def _remote_control(cfg) -> bool:
    return load_config(str(cfg)).settings.link.remote_control


def _pair(runner, relay, cfg):
    phone = new_device()
    relay.stage_pairing(claimed_devices=[_claimed(phone, qr=True)])
    result = _link(runner, cfg, "--no-qr", "--no-remote-control")
    assert result.exit_code == 0, result.output
    return phone


@contextlib.contextmanager
def _serve_runs_the_link(*, port: int, config: str):
    """Play the magent serve that runs the link: hold LINK_LOCK and write the
    record its link thread writes while it holds it."""
    with lockfile.persistent_lock(LINK_LOCK, wait_s=0.0):
        linkfiles.save_serve({"pid": 4242, "port": port, "config": config})
        try:
            yield
        finally:
            linkfiles.clear_serve()


class TestPairing:
    def test_the_qr_path_pairs_without_a_prompt(self, runner, relay, cfg):
        phone = new_device()
        relay.stage_pairing(claimed_devices=[_claimed(phone, qr=True)])

        result = _link(runner, cfg, "--no-qr", "--no-remote-control")

        assert result.exit_code == 0, result.output
        out = result.output
        assert "Pair this machine with the magent app." in out
        assert f"Open this link on the device: {relay.url}/pair#c=ABCDEFGH&k=" in out
        assert f"or open {relay.url}/pair and enter the code ABCD-EFGH." in out
        assert f"Paired {phone.name} (QR verified)." in out
        assert "Remote control: off." in out
        assert "This machine is linked as hst_fake_1." in out
        assert "Does the app show" not in out
        saved = linkfiles.load_link()
        assert saved is not None
        assert (saved.relay_url, saved.host_id, saved.host_token) == (
            relay.url,
            "hst_fake_1",
            relay.host_token,
        )
        assert [d.device_id for d in saved.devices] == [phone.device_id]
        state = linkfiles.load_state()
        assert (state.state, state.cursor, state.seen_ids, state.host_id) == (
            "connecting",
            0,
            (),
            "hst_fake_1",
        )
        assert _remote_control(cfg) is False
        assert relay.host_token not in out
        assert not relay.deleted

    def test_the_typed_code_path_confirms_the_safety_number_then_asks_about_remote_control(
        self, runner, relay, cfg
    ):
        phone = new_device()
        relay.stage_pairing(claimed_devices=[_claimed(phone, qr=False)])

        result = _link(runner, cfg, "--no-qr", input="y\ny\n")

        assert result.exit_code == 0, result.output
        out = result.output
        saved = linkfiles.load_link()
        host = linkcrypto.public_from_private(
            linkcrypto.b64u_decode(saved.host_kex_priv),
            linkcrypto.b64u_decode(saved.host_sign_priv),
        )
        # The number's order is the app's: host keys first, then the device's.
        number = linkcrypto.safety_number(
            host.kex_pub, host.sign_pub, phone.keys.kex_pub, phone.keys.sign_pub
        )
        assert ANSWER_HELP in out
        assert f"Does the app show {number}? [y/N]: y" in out
        rc_prompt = (
            "Allow devices paired with this host to send text, choose, interrupt, "
            "change model, start/stop sessions and edit projects? [y/N]: y"
        )
        assert rc_prompt in out
        # Remote control is asked only after the numbers matched.
        assert out.index("Does the app show") < out.index(rc_prompt)
        assert f"Paired {phone.name} (safety number confirmed)." in out
        assert "Remote control: on." in out
        assert _remote_control(cfg) is True

    def test_an_unconfirmed_safety_number_pairs_nothing_and_frees_the_host(
        self, runner, relay, cfg
    ):
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=False)])

        result = _link(runner, cfg, "--no-qr", input="n\n")

        assert result.exit_code == 1
        assert (
            "the safety numbers were not confirmed; nothing was paired" in result.output
        )
        assert "Allow devices" not in result.output
        assert linkfiles.load_link() is None
        assert not linkfiles.LINK_STATE_PATH.exists()
        assert relay.deleted  # the relay is told the pairing failed
        assert _remote_control(cfg) is False

    def test_ctrl_c_at_the_number_frees_the_host(self, runner, relay, cfg):
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=False)])

        result = _link(runner, cfg, "--no-qr", input="")  # EOF = Ctrl+C at the prompt

        assert result.exit_code == 1
        assert linkfiles.load_link() is None
        assert relay.deleted

    def test_the_flag_skips_the_remote_control_question(self, runner, relay, cfg):
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=True)])
        result = _link(runner, cfg, "--no-qr", "--remote-control")
        assert result.exit_code == 0, result.output
        assert "Allow devices" not in result.output
        assert _remote_control(cfg) is True

    def test_the_qr_is_drawn_by_default(self, runner, relay, cfg, monkeypatch):
        drawn: list[str] = []
        monkeypatch.setattr("magent.cli.ui._print_qr", drawn.append)
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=True)])
        result = _link(runner, cfg, "--no-remote-control")
        assert result.exit_code == 0, result.output
        assert drawn and drawn[0].startswith(f"{relay.url}/pair#c=ABCDEFGH&k=")
        assert "Scan the QR code with the device's camera," in result.output
        assert drawn[0] not in result.output  # the link (and its secret) only in the QR

    def test_an_expired_code_says_so(self, runner, relay, cfg):
        relay.expire_pairing()
        result = _link(runner, cfg, "--no-qr", "--no-remote-control")
        assert result.exit_code == 1
        assert (
            "The code expired before a device claimed it. Run `magent link` again."
            in result.output
        )
        assert linkfiles.load_link() is None

    def test_a_relay_too_new_says_upgrade(self, runner, relay, cfg):
        relay.set_protocol_too_old()
        result = _link(runner, cfg, "--no-qr")
        assert result.exit_code == 1
        assert UPGRADE_HINT in result.output

    def test_a_rate_limited_relay_is_shown_verbatim(self, runner, relay, cfg):
        relay.rate_limit_next_calls(1, retry_after=120)
        result = _link(runner, cfg, "--no-qr")
        assert result.exit_code == 1
        assert "the relay is limiting new pairings; try again in 2 min" in result.output

    def test_a_lost_host_token_points_to_the_hosts_page(self, runner, relay, cfg):
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=True)])
        relay._token_taken = True  # the one reply that carried it was lost
        result = _link(runner, cfg, "--no-qr", "--no-remote-control")
        assert result.exit_code == 1
        assert "remove this host on the Hosts page of the app first" in result.output
        assert linkfiles.load_link() is None

    def test_another_app_is_refused_with_the_setting_to_change(
        self, runner, relay, cfg, monkeypatch
    ):
        from magent import env

        monkeypatch.setenv("MAGENT_LINK_APP_URL", "https://app.example")
        monkeypatch.setattr(env, "_cached_env", None)
        result = _link(runner, cfg, "--no-qr")
        assert result.exit_code == 1
        assert (
            "this relay's app is not https://app.example; set MAGENT_LINK_APP_URL "
            "if you run your own app"
        ) in result.output

    def test_a_broken_environment_dials_nothing(self, runner, relay, cfg, monkeypatch):
        from magent import env

        # link.relay_url() would fall back to the production relay on a
        # MAGENT_* environment that does not validate; the CLI stops first.
        monkeypatch.setenv("MAGENT_LINK", "DECOY-not-a-bool")
        monkeypatch.setattr(env, "_cached_env", None)
        result = _link(runner, cfg, "--no-qr")
        assert result.exit_code == 1
        assert relay.requests == []

    def test_without_the_link_extra_it_prints_the_install_hint(
        self, runner, relay, cfg, monkeypatch
    ):
        def missing() -> None:
            raise linkcrypto.LinkCryptoUnavailable("no cryptography")

        monkeypatch.setattr(linkcrypto, "_require", missing)
        result = _link(runner, cfg, "--no-qr")
        assert result.exit_code == 1
        assert 'pip install "magent-multi-ai-agents-manager[link]"' in result.output
        assert relay.pair_starts == []

    @pytest.mark.parametrize(
        ("expires_in", "waits"),
        [
            (24 * 3600.0, link_cmd.PAIR_WAIT_S + link_cmd.PAIR_WAIT_SLACK_S),
            (120.0, 120.0),
        ],
    )
    def test_the_wait_ends_at_the_code_expiry_or_ten_minutes(
        self, runner, relay, cfg, monkeypatch, expires_in, waits
    ):
        relay.expires_at = _iso(time.time() + expires_in)
        deadlines: list[float] = []

        def wait(client, start, *, deadline):
            deadlines.append(deadline)
            raise linkpair.PairingExpired("no device claimed the code in time")

        monkeypatch.setattr(linkpair, "wait_for_claim", wait)
        began = time.time()
        _link(runner, cfg, "--no-qr")
        [deadline] = deadlines
        assert began + waits - 5 <= deadline <= time.time() + waits

    def test_the_device_name_is_shown_escaped_and_bounded(self, runner, relay, cfg):
        phone = new_device(name="Caf\u00e9\\" + "x" * 90)
        relay.stage_pairing(claimed_devices=[_claimed(phone, qr=True)])
        result = _link(runner, cfg, "--no-qr", "--no-remote-control")
        assert result.exit_code == 0, result.output
        shown = "Caf\\xe9\\\\" + "x" * (link_cmd.NAME_SHOWN_MAX - 9) + "..."
        assert f"Paired {shown} (QR verified)." in result.output
        assert phone.name not in result.output
        # link.json keeps the name as the device sent it.
        assert linkfiles.load_link().devices[0].name == phone.name

    @pytest.mark.parametrize("fault", [OSError("disk full"), KeyboardInterrupt()])
    def test_a_host_that_cannot_be_saved_is_freed_on_the_relay(
        self, runner, relay, cfg, monkeypatch, fault
    ):
        relay.stage_pairing(claimed_devices=[_claimed(new_device(), qr=True)])

        def save_link(*_a, **_k):
            raise fault

        monkeypatch.setattr(linkfiles, "save_link", save_link)
        result = _link(runner, cfg, "--no-qr", "--no-remote-control")
        assert result.exit_code == 1
        assert relay.deleted
        assert not linkfiles.LINK_PATH.exists()
        assert relay.host_token not in result.output


class TestLinked:
    def test_linking_again_says_already_linked(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        starts = len(relay.pair_starts)
        result = _link(runner, cfg)
        assert result.exit_code == 0
        assert (
            f"This machine is already linked (host hst_fake_1 on {relay.url})."
            in result.output
        )
        assert (
            "Pair another device with `magent link --add-device`, or start over with `magent unlink`."
            in result.output
        )
        assert len(relay.pair_starts) == starts

    def test_the_relay_url_is_shown_without_its_userinfo(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        saved = linkfiles.load_link()
        linkfiles.save_link(
            type(saved)(
                **{**saved.__dict__, "relay_url": "http://DECOY:pw_DECOY@127.0.0.1:9"}
            )
        )
        result = _link(runner, cfg)
        assert "(host hst_fake_1 on http://127.0.0.1:9)." in result.output
        assert "pw_DECOY" not in result.output

    def test_the_remote_control_flag_alone_only_flips_the_setting(
        self, runner, relay, cfg
    ):
        _pair(runner, relay, cfg)
        result = _link(runner, cfg, "--remote-control")
        assert result.exit_code == 0
        assert result.output.strip() == "Remote control: on."
        assert _remote_control(cfg) is True

    def test_the_flag_says_when_the_link_serve_reads_another_config(
        self, runner, relay, cfg, tmp_path
    ):
        _pair(runner, relay, cfg)
        other = tmp_path / "elsewhere" / "magent.config.json"
        with _serve_runs_the_link(port=8033, config=str(other)):
            result = _link(runner, cfg, "--remote-control")
        assert result.exit_code == 0
        assert "Remote control: on." in result.output
        assert f"the magent serve running the link reads {other}" in result.output
        assert _remote_control(cfg) is True

    def test_add_device_pairs_one_more_with_the_same_host(self, runner, relay, cfg):
        phone = _pair(runner, relay, cfg)
        tablet = new_device(TABLET_ID, "Tablet DECOY")
        relay.stage_pairing(claimed_devices=[_claimed(tablet, qr=True)], new_host=False)

        result = _link(runner, cfg, "--add-device", "--no-qr")

        assert result.exit_code == 0, result.output
        _method, _path, headers = [
            r for r in relay.requests if r[1] == "/v1/host/pair/start"
        ][-1]
        assert headers["Authorization"] == f"Bearer {relay.host_token}"
        saved = linkfiles.load_link()
        assert [d.device_id for d in saved.devices] == [
            phone.device_id,
            tablet.device_id,
        ]
        assert saved.host_token == relay.host_token
        assert "Allow devices" not in result.output  # add-device keeps the setting
        assert f"Paired {tablet.name} (QR verified)." in result.output

    def test_a_declined_add_device_releases_nothing(self, runner, relay, cfg):
        phone = _pair(runner, relay, cfg)
        relay.stage_pairing(
            claimed_devices=[_claimed(new_device(TABLET_ID), qr=False)],
            new_host=False,
        )
        result = _link(runner, cfg, "--add-device", "--no-qr", input="n\n")
        assert result.exit_code == 1
        assert not relay.deleted  # that would unlink the host itself
        assert [d.device_id for d in linkfiles.load_link().devices] == [phone.device_id]

    def test_add_device_for_another_host_saves_nothing(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        saved = linkfiles.load_link()
        linkfiles.save_link(type(saved)(**{**saved.__dict__, "host_id": "hst_DECOY_2"}))
        relay.stage_pairing(
            claimed_devices=[_claimed(new_device(TABLET_ID), qr=True)], new_host=False
        )
        result = _link(runner, cfg, "--add-device", "--no-qr")
        assert result.exit_code == 1
        assert "nothing was saved" in result.output
        assert [d.device_id for d in linkfiles.load_link().devices] == [PHONE_ID]
        assert not relay.deleted

    def test_add_device_keeps_at_most_ten_devices(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        saved = linkfiles.load_link()
        ten = tuple(
            linkfiles.TrustedDevice(
                device_id=f"0190f3a2-7b1c-7d4e-8a5b-00000000000{n}",
                name=f"Old DECOY {n}",
                kex_pub="k",
                sign_pub="s",
                paired_at="2026-10-01T00:00:00+00:00",
            )
            for n in range(10)
        )
        linkfiles.save_link(type(saved)(**{**saved.__dict__, "devices": ten}))
        relay.stage_pairing(
            claimed_devices=[_claimed(new_device(OTHER_ID, "New DECOY"), qr=True)],
            new_host=False,
        )
        result = _link(runner, cfg, "--add-device", "--no-qr")
        assert result.exit_code == 0, result.output
        ids = [d.device_id for d in linkfiles.load_link().devices]
        assert len(ids) == link_cmd.DEVICES_MAX == 10
        assert ids[-1] == OTHER_ID
        assert ten[0].device_id not in ids
        assert "Old DECOY 0" in result.output  # the one it let go, by name

    def test_add_device_needs_a_link(self, runner, relay, cfg):
        result = _link(runner, cfg, "--add-device")
        assert result.exit_code == 1
        assert (
            "This machine is not linked yet; run `magent link` first." in result.output
        )

    def test_a_revoked_link_pairs_afresh(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        linkfiles.save_state(
            linkfiles.LinkState(
                state="revoked", cursor=41, seen_ids=("old",), host_id="hst_fake_1"
            )
        )
        relay.stage_pairing(claimed_devices=[_claimed(new_device(OTHER_ID), qr=True)])
        result = _link(runner, cfg, "--no-qr", "--no-remote-control")
        assert result.exit_code == 0, result.output
        assert [d.device_id for d in linkfiles.load_link().devices] == [OTHER_ID]
        state = linkfiles.load_state()
        assert (state.state, state.cursor, state.seen_ids) == ("connecting", 0, ())


class TestStatus:
    def test_json_while_disabled(self, runner, cfg):
        result = _link(runner, cfg, "--status", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "ok": True,
            "link": {"state": "disabled", "retry_in_s": None},
            "relay_url": None,
            "host_id": None,
            "devices": [],
            "remote_control": False,
        }

    def test_a_linked_machine_lists_its_devices_and_never_the_token(
        self, runner, relay, cfg, monkeypatch
    ):
        from magent import env

        phone = _pair(runner, relay, cfg)
        monkeypatch.setenv("MAGENT_LINK", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        monkeypatch.setattr(link_cmd, "_serving", lambda _cfg: False)
        result = _link(runner, cfg, "--status")
        assert result.exit_code == 0
        assert "offline (magent serve is not running)" in result.output
        assert "hst_fake_1" in result.output
        assert phone.name in result.output
        assert relay.host_token not in result.output

    def test_a_serve_that_stands_by_is_named(self, runner, relay, cfg, monkeypatch):
        from magent import env

        _pair(runner, relay, cfg)
        monkeypatch.setenv("MAGENT_LINK", "1")
        monkeypatch.setattr(env, "_cached_env", None)
        monkeypatch.setattr(link_cmd, "_serving", lambda _cfg: True)
        with _serve_runs_the_link(port=9123, config=str(cfg)):
            result = _link(runner, cfg, "--status")
        assert result.exit_code == 0
        assert "connecting" in result.output
        assert (
            "the magent serve on port 9123 runs the link; the one on port 8033 "
            "stands by"
        ) in result.output

    def test_json_goes_with_status_only(self, runner, cfg):
        result = _link(runner, cfg, "--json")
        assert result.exit_code == 2


class TestUnlink:
    def test_unlink_tells_the_relay_and_forgets(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        lock = lockfile.lock_path(LINK_LOCK)
        with lockfile.persistent_lock(LINK_LOCK, wait_s=0.0):
            pass  # a serve that once ran the link left its sidecar
        result = runner.invoke(cli.main, ["--config", str(cfg), "unlink"])
        assert result.exit_code == 0
        assert result.output.strip() == "Unlinked. The relay revoked this machine."
        assert relay.deleted
        assert linkfiles.load_link() is None
        assert not linkfiles.LINK_STATE_PATH.exists()
        assert lock.exists()  # the lock sidecar is never deleted

    def test_an_unreachable_relay_still_unlinks_here(self, runner, relay, cfg):
        _pair(runner, relay, cfg)
        saved = linkfiles.load_link()
        linkfiles.save_link(
            type(saved)(**{**saved.__dict__, "relay_url": "http://127.0.0.1:9"})
        )
        result = runner.invoke(cli.main, ["--config", str(cfg), "unlink"])
        assert result.exit_code == 0
        assert "Unlinked here, but the relay could not be told" in result.output
        assert "Revoke this machine from the app's Hosts page." in result.output
        assert linkfiles.load_link() is None

    def test_unlink_when_not_linked(self, runner, cfg):
        result = runner.invoke(cli.main, ["--config", str(cfg), "unlink"])
        assert result.exit_code == 0
        assert result.output.strip() == "This machine is not linked."

    def test_unlink_clears_left_over_pulls_unless_a_serve_runs_the_link(
        self, runner, relay, cfg
    ):
        _pair(runner, relay, cfg)
        stale = linkfiles.INCOMING_DIR / f"{PHONE_ID}.0a1b2c3d"
        (stale / "photo.png").parent.mkdir(parents=True)
        (stale / "photo.png").write_bytes(b"DECOY")
        with _serve_runs_the_link(port=8033, config=str(cfg)):
            result = runner.invoke(cli.main, ["--config", str(cfg), "unlink"])
        assert result.exit_code == 0
        assert stale.exists()  # a live puller may own it
        _pair(runner, relay, cfg)
        result = runner.invoke(cli.main, ["--config", str(cfg), "unlink"])
        assert result.exit_code == 0
        assert not stale.exists()
