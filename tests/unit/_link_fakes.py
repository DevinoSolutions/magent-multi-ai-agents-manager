"""Fakes shared by the link runner tests: a paired host on disk, app-side
device keys that sign and seal commands the way magent-app does, a scripted
LinkHost, and a runner on a background thread.

Every token here carries DECOY (gitleaks allowlists it under tests/). Ids are
what the relay hands out: device, command and upload ids are canonical UUIDs.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from magent import linkcrypto, linkfiles
from magent.link import LinkRunner, _default_client
from magent.linkcrypto import AAD_CMD, AAD_RES, AAD_UKEY, AAD_UMETA, KeyPairs, PairKeys
from magent.linkfiles import LinkFile, TrustedDevice

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from magent.link import ClientFactory
    from tests._fake_relay import FakeRelay

HOST_ID = "hst_fake_1"  # what tests/_fake_relay.py's pairing hands out
PAIRED_AT = "2026-10-08T12:00:00+00:00"
PHONE_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a01"
TABLET_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a02"
STRANGER_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a03"
OTHER_ID = "0190f3a2-7b1c-7d4e-8a5b-3c2d1e0f9a04"


def cmd_id(n: int) -> str:
    """The n-th test command id: a canonical v4 UUID, as the relay requires."""
    return f"00000000-0000-4000-8000-{n:012d}"


@dataclass(frozen=True)
class Device:
    """One app device: its id, name and both key pairs."""

    device_id: str
    name: str
    keys: KeyPairs

    def trusted(self) -> TrustedDevice:
        return TrustedDevice(
            device_id=self.device_id,
            name=self.name,
            kex_pub=linkcrypto.b64u_encode(self.keys.kex_pub),
            sign_pub=linkcrypto.b64u_encode(self.keys.sign_pub),
            paired_at=PAIRED_AT,
        )

    def wire(self, revoked_at: str | None = None) -> dict[str, object]:
        """The device as the relay lists it (C4 TrustedDevice)."""
        return {
            "deviceId": self.device_id,
            "name": self.name,
            "kexPub": linkcrypto.b64u_encode(self.keys.kex_pub),
            "signPub": linkcrypto.b64u_encode(self.keys.sign_pub),
            "pairedAt": PAIRED_AT,
            "revokedAt": revoked_at,
        }


def new_device(device_id: str = PHONE_ID, name: str = "Phone DECOY") -> Device:
    return Device(device_id, name, linkcrypto.generate_key_pairs())


@dataclass(frozen=True)
class Linked:
    """A host paired on disk: link.json + link-state.json under tmp_path."""

    host: KeyPairs
    devices: tuple[Device, ...]
    link_path: Path
    state_path: Path
    relay_url: str

    def device_keys(self, device: Device) -> PairKeys:
        """The device's side of the pair keys (equal to the host's side)."""
        return linkcrypto.pair_keys(
            device.keys.kex_priv, self.host.kex_pub, HOST_ID, device.device_id
        )

    def state(self) -> linkfiles.LinkState:
        return linkfiles.load_state(self.state_path)

    def link(self) -> LinkFile | None:
        return linkfiles.load_link(self.link_path)

    def device_ids(self) -> list[str] | None:
        """The trusted device ids on disk, or None while the file is being
        replaced (Windows refuses a read during the runner's atomic swap)."""
        try:
            link = self.link()
        except linkfiles.LinkFileError:
            return None
        return None if link is None else [d.device_id for d in link.devices]


def write_link(
    tmp_path: Path,
    relay_url: str,
    devices: tuple[Device, ...],
    *,
    host_token: str,
    write: bool = True,
) -> Linked:
    host = linkcrypto.generate_key_pairs()
    linked = Linked(
        host=host,
        devices=devices,
        link_path=tmp_path / "link.json",
        state_path=tmp_path / "link-state.json",
        relay_url=relay_url,
    )
    if write:
        save_linked(linked, host_token=host_token)
    return linked


def save_linked(
    linked: Linked,
    *,
    host_token: str,
    devices: tuple[TrustedDevice, ...] | None = None,
) -> None:
    linkfiles.save_link(
        LinkFile(
            relay_url=linked.relay_url,
            host_id=HOST_ID,
            host_token=host_token,
            host_kex_priv=linkcrypto.b64u_encode(linked.host.kex_priv),
            host_sign_priv=linkcrypto.b64u_encode(linked.host.sign_priv),
            devices=devices
            if devices is not None
            else tuple(d.trusted() for d in linked.devices),
        ),
        linked.link_path,
    )


def make_command(
    linked: Linked,
    device: Device,
    *,
    cid: str,
    seq: int,
    method: str = "GET",
    path: str = "/api/v1/sessions",
    body: object | None = None,
    ts: object | None = None,
    inner: dict[str, object] | None = None,
    signer: Device | None = None,
    seal_key: bytes | None = None,
) -> dict[str, object]:
    """A HostCommand (C4) exactly as magent-app builds one. ``inner``,
    ``signer`` and ``seal_key`` forge the three ways a command goes bad; a
    ``ts`` that is not an int is sent as given, signed over its text."""
    keys = linked.device_keys(device)
    plain = (
        inner if inner is not None else {"method": method, "path": path, "body": body}
    )
    sealed = linkcrypto.seal(
        seal_key if seal_key is not None else keys.d2h,
        json.dumps(plain).encode(),
        AAD_CMD + cid,
    )
    stamp = ts if ts is not None else int(time.time() * 1000)
    # Signed over whatever the fields are, so a malformed command still
    # carries a good signature: only the field checks can refuse it.
    message = linkcrypto.command_signing_input(
        cid, HOST_ID, method, path, stamp, sealed
    )
    sig = linkcrypto.sign((signer or device).keys.sign_priv, message)
    return {
        "id": cid,
        "seq": seq,
        "deviceId": device.device_id,
        "method": method,
        "path": path,
        "sealed": sealed,
        "sig": sig,
        "ts": stamp,
        "expiresAt": "2026-10-08T23:59:00.000Z",
    }


def open_result(
    linked: Linked, device: Device, result: dict[str, object]
) -> dict[str, object]:
    """A command result as the device reads it: ``{status, body}``."""
    keys = linked.device_keys(device)
    plain = linkcrypto.open_sealed(
        keys.h2d, str(result["sealed"]), AAD_RES + str(result["commandId"])
    )
    return json.loads(plain)


def make_upload(
    relay: FakeRelay,
    linked: Linked,
    device: Device,
    *,
    upload_id: str,
    data: bytes,
    name: str = "photo.png",
    session: str = "magent-alpha",
    inject: bool = True,
    size: int | None = None,
    tamper: bool = False,
) -> dict[str, object]:
    """An UploadReady (C4) for ``data``, its encrypted object served by the
    fake relay at ``/blob/<upload_id>`` -- as magent-app stages one."""
    keys = linked.device_keys(device)
    content_key = os.urandom(32)
    blob = bytearray(linkcrypto.encrypt_upload(content_key, upload_id, data))
    if tamper:
        blob[20] ^= 0x01
    relay.objects[upload_id] = bytes(blob)
    meta = {"name": name, "type": "image/png", "size": len(data)}
    return {
        "uploadId": upload_id,
        "deviceId": device.device_id,
        "session": session,
        "sizeBytes": len(blob) if size is None else size,
        "getUrl": f"{relay.url}/blob/{upload_id}",
        "wrappedKey": linkcrypto.seal(keys.d2h, content_key, AAD_UKEY + upload_id),
        "sealedMeta": linkcrypto.seal(
            keys.d2h, json.dumps(meta).encode(), AAD_UMETA + upload_id
        ),
        "inject": inject,
    }


class FakeFeed:
    """An EventFeed fed by the test."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, object]] = queue.Queue()
        self.closed = False

    def push(self, *events: dict[str, object]) -> None:
        for event in events:
            self._queue.put(event)

    def next_batch(self, timeout: float) -> list[dict[str, object]]:
        # Capped so a stopping forwarder notices within 50 ms (tests only).
        try:
            out = [self._queue.get(timeout=min(timeout, 0.05))]
        except queue.Empty:
            return []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeHost:
    """A LinkHost that records every call and answers from a script.

    ``on_handle`` runs inside ``handle`` before it answers, for tests that
    look at what was on disk at the moment a command ran.
    """

    sessions: list[dict[str, object]] = field(default_factory=list)
    rc: bool = False
    max_bytes: int = 1024 * 1024
    calls: list[tuple[str, str, bytes | None, str]] = field(default_factory=list)
    replies: dict[tuple[str, str], tuple[int, dict[str, object]]] = field(
        default_factory=dict
    )
    feed: FakeFeed = field(default_factory=FakeFeed)
    staged: list[tuple[str, str, str, bytes, bool]] = field(default_factory=list)
    stage_reply: dict[str, object] | None = None
    on_handle: Callable[[str, str], None] | None = None
    daemon: dict[str, str] = field(
        default_factory=lambda: {
            "version": "9.9.9",
            "platform": "windows",
            "epoch": "e0e0e0e0",
        }
    )

    def handle(
        self, method: str, path: str, body: bytes | None, device_id: str
    ) -> tuple[int, dict[str, object]]:
        if self.on_handle is not None:
            self.on_handle(method, path)
        self.calls.append((method, path, body, device_id))
        return self.replies.get(
            (method, path), (200, {"ok": True, "data": {"path": path}})
        )

    def stage_upload(
        self, device_id: str, session: str, file_path: Path, inject: bool
    ) -> dict[str, object]:
        self.staged.append(
            (device_id, session, file_path.name, file_path.read_bytes(), inject)
        )
        if self.stage_reply is not None:
            return self.stage_reply
        return {"ok": True, "data": {"paste": "injected" if inject else "saved"}}

    def snapshot(self) -> tuple[list[dict[str, object]], dict[str, str]]:
        return list(self.sessions), dict(self.daemon)

    def subscribe(self) -> FakeFeed:
        return self.feed

    def remote_control(self) -> bool:
        return self.rc

    def upload_max_bytes(self) -> int:
        return self.max_bytes


# A SessionRow (fleetview.SessionRow.to_dict()) cut to the fields that matter
# here: the routing ones and two that must never reach the relay in clear.
SESSION_ROW: dict[str, object] = {
    "session": "magent-alpha",
    "name": "alpha",
    "path": "C:/work/alpha",
    "live": True,
    "hook_state": "working",
    "pane_state": "busy",
    "node_state": None,
    "model": "opus",
}


class Running:
    """A LinkRunner on a background thread, with fast backoff sleeps.

    ``sleeps`` records every wait the runner asked for, before the cap.
    Every instance is listed in ``live`` until stop_all() has joined it.
    """

    live: ClassVar[list[Running]] = []

    def __init__(
        self,
        host: FakeHost,
        linked: Linked,
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.stop = threading.Event()
        self.outcome: list[str] = []
        self.sleeps: list[float] = []
        self.runner = LinkRunner(
            host,
            client_factory=client_factory or _default_client,
            clock=clock,
            sleep=self._sleep,
            link_path=linked.link_path,
            state_path=linked.state_path,
        )
        self.thread = threading.Thread(
            target=lambda: self.outcome.append(self.runner.run(self.stop)), daemon=True
        )
        Running.live.append(self)
        self.thread.start()

    def _sleep(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        return self.stop.wait(min(seconds, 0.02))

    def wait_done(self, timeout: float = 5.0) -> str:
        """The terminal state, for a run that ends on its own."""
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the runner did not end"
        return self.outcome[0]

    def finish(self, relay: FakeRelay) -> str:
        """Stop the runner and join it. A stream only notices ``stop`` at its
        next frame, so its connection is cut until the thread is gone (a
        stream that was still opening misses the first cut)."""
        self.stop.set()
        deadline = time.monotonic() + 5.0
        while self.thread.is_alive() and time.monotonic() < deadline:
            relay.drop_streams()
            self.thread.join(0.05)
        return self.wait_done(0)


def stop_all(timeout: float = 5.0) -> list[Running]:
    """Stop and join every runner a test left running; the ones still alive.

    A test that fails before finish() would otherwise leave its runner
    logging on a daemon thread, and a thread frozen mid-log at interpreter
    exit can hold the log handler's lock that logging's shutdown then waits
    on. Run after the relay is closed, so an open stream has already ended.
    """
    stuck: list[Running] = []
    while Running.live:
        run = Running.live.pop()
        run.stop.set()
        run.thread.join(timeout)
        if run.thread.is_alive():
            stuck.append(run)
    return stuck
