"""magent link end to end, in process: `magent link` pairs against the fake
relay, then serve's link thread (link.supervise -> LinkRunner ->
ApiLinkHost -> api.handle, nothing faked between them) answers a signed
command, survives a dropped stream, pulls an upload exactly once even when
the relay replays it, and stops when the relay revokes the host."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from magent import (
    api,
    cli,
    env,
    events,
    fleetview,
    link,
    linkcrypto,
    linkfiles,
    linkpair,
)
from magent.link_host import ApiLinkHost
from magent.linkcrypto import AAD_SNAP
from tests.unit._link_fakes import (
    HOST_ID,
    Linked,
    cmd_id,
    cut_stream_and_wait,
    make_command,
    make_upload,
    new_device,
    open_result,
    up_id,
)

SECRET = b"\x05" * 16  # the QR secret, pinned so this test can play the app


def _row(session: str) -> fleetview.SessionRow:
    return fleetview.SessionRow(
        session=session,
        name="alpha",
        path="work/alpha",
        cwd=None,
        group=None,
        tool=None,
        enabled=True,
        node=None,
        live=True,
        hook_state="working",
        hook_state_ts=1.0,
        hook_state_age_s=1.0,
        hook_state_stale=False,
        pane_state="busy",
        pane_state_ts=1.0,
        node_state=None,
        model="Opus",
        effort="high",
        session_id="abc",
    )


@pytest.fixture
def relay(fake_relay, monkeypatch):
    """The conftest fake relay (its real pair/start shape, MAGENT_LINK_URL and
    MAGENT_LINK_APP_URL aimed at it), with the link on in this process only
    and the pairing code good for the relay's ten minutes."""
    monkeypatch.setenv("MAGENT_LINK", "1")
    monkeypatch.setattr(env, "_cached_env", None)
    fake_relay.expires_at = datetime.fromtimestamp(
        time.time() + 600, timezone.utc
    ).isoformat()
    return fake_relay


def _states(relay, upload_id: str) -> list[str]:
    return [
        str(r["state"])
        for r in list(relay.results)
        if r.get("type") == "upload" and r.get("uploadId") == upload_id
    ]


def test_pair_serve_command_upload_replay_revoke(runner, relay, tmp_path, monkeypatch):
    # -- `magent link`: the app scans the QR (the secret is pinned so this test can play the app)
    monkeypatch.setattr(linkpair.secrets, "token_bytes", lambda n: SECRET[:n])
    config = tmp_path / "magent.config.json"
    config.write_text(json.dumps({"version": 4, "projects": []}), encoding="utf-8")
    phone = new_device()
    relay.stage_pairing(
        claimed_devices=[
            {
                "deviceId": phone.device_id,
                "name": phone.name,
                "kexPub": linkcrypto.b64u_encode(phone.keys.kex_pub),
                "signPub": linkcrypto.b64u_encode(phone.keys.sign_pub),
                "mac": linkcrypto.pair_mac(
                    SECRET, phone.keys.kex_pub, phone.keys.sign_pub
                ),
            }
        ]
    )
    result = runner.invoke(
        cli.main, ["--config", str(config), "link", "--no-qr", "--no-remote-control"]
    )
    assert result.exit_code == 0, result.output
    saved = linkfiles.load_link()
    assert saved is not None
    host_keys = linkcrypto.public_from_private(
        linkcrypto.b64u_decode(saved.host_kex_priv),
        linkcrypto.b64u_decode(saved.host_sign_priv),
    )
    linked = Linked(
        host=host_keys,
        devices=(phone,),
        link_path=linkfiles.LINK_PATH,
        state_path=linkfiles.LINK_STATE_PATH,
        relay_url=relay.url,
    )

    # -- `magent serve`'s link thread over a real api.ApiContext
    monkeypatch.setattr(fleetview, "rows", lambda *_a, **_k: [_row("magent-alpha")])
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
    pastes: list[tuple[str, str]] = []
    ctx = api.ApiContext(
        config_path=str(config),
        bus=events.EventBus("e2e0e2e0"),
        allowed_hosts=frozenset({"127.0.0.1"}),
        upload_dir=tmp_path / "uploads",
        upload_max_bytes=10 * 1024 * 1024,
        request_limit=11 * 1024 * 1024,
        health=lambda: api.Health("magent-upload", 8033, 1, 1.0, 1, 0.1),
        upload_sessions=lambda: {"magent-alpha"},
        inject_fn=lambda session, text: (pastes.append((session, text)) or True, False),
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=link.supervise,
        args=(lambda: ApiLinkHost(lambda: ctx, str(config)), stop),
        kwargs={
            "runner_factory": lambda host: link.LinkRunner(
                host, sleep=lambda s: stop.wait(min(s, 0.05))
            )
        },
        daemon=True,
        name=link.LINK_THREAD,
    )
    thread.start()
    upload = up_id(39)
    try:
        # -- the snapshot: routing metadata in the clear, the rows sealed for the phone
        relay.wait_for(lambda: relay.snapshots)
        snap = relay.snapshots[0]
        assert snap["remoteControl"] is False
        assert snap["uploadMaxBytes"] == 10 * 1024 * 1024
        assert [s["session"] for s in snap["sessions"]] == ["magent-alpha"]
        rows = json.loads(
            linkcrypto.open_sealed(
                linked.device_keys(phone).h2d,
                snap["sealed"][phone.device_id],
                AAD_SNAP + HOST_ID,
            )
        )["sessions"]
        assert rows[0]["model"] == "Opus"
        assert "work/alpha" not in json.dumps(snap["sessions"])  # paths stay sealed

        # -- a signed command, answered through api.handle, readable only by the phone
        relay.queue_command(
            make_command(linked, phone, cid=cmd_id(1), seq=1, path="/api/v1/sessions")
        )
        relay.wait_for(lambda: relay.results)
        answer = open_result(linked, phone, relay.results[0])
        assert answer["status"] == 200
        assert answer["body"]["data"]["sessions"][0]["session"] == "magent-alpha"

        # -- a write while remote control is off: refused by api.handle itself
        relay.queue_command(
            make_command(
                linked,
                phone,
                cid=cmd_id(2),
                seq=2,
                method="POST",
                path="/api/v1/sessions/stop",
                body={"sessions": ["magent-alpha"]},
            )
        )
        relay.wait_for(lambda: len(relay.results) == 2)
        refused = open_result(linked, phone, relay.results[1])
        assert refused["status"] == 403
        assert refused["body"]["error"]["details"]["reason"] == "remote_control_off"

        # -- the stream drops; the runner reconnects from its cursor
        relay.wait_for(lambda: linkfiles.load_state().cursor == 2)
        relay.wait_for(
            lambda: (
                (relay.drop_streams() or True)
                and any(
                    p == "/v1/stream/host?since=2" for _m, p, _h in list(relay.requests)
                )
            )
        )

        # -- an upload: pulled, decrypted, saved, pasted once
        data = b"\x89PNG DECOY image bytes"
        relay.queue_upload_ready(
            make_upload(relay, linked, phone, upload_id=upload, data=data)
        )
        relay.wait_for(lambda: _states(relay, upload) == ["pulled", "injected"])
        [saved_file] = list((tmp_path / "uploads").iterdir())
        assert saved_file.read_bytes() == data
        assert len(pastes) == 1

        # -- the relay loses the results and replays upload.ready: no second paste
        with relay._cond:
            relay.results.clear()
        cut_stream_and_wait(relay, lambda: _states(relay, upload))
        assert _states(relay, upload) == ["injected"]
        assert len(pastes) == 1
        assert len(list((tmp_path / "uploads").iterdir())) == 1

        # -- the host is revoked: the runner stops, the state says so
        relay.revoke()
        relay.wait_for(lambda: linkfiles.load_state().state == "revoked")
        assert thread.is_alive()  # supervise now waits for a new `magent link`
    finally:
        stop.set()
        thread.join(15)
    assert not thread.is_alive()
    assert relay.host_token not in result.output
