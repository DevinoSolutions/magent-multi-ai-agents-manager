"""The link's two files under ~/.magent (contract C8).

``link.json`` is a secret: the relay URL, this host's id and token, both
private keys and the devices it trusts. It is written owner-only
(fsutil.write_private) and refused on read unless it is still owner-only and
exactly the expected shape -- the repair is always `magent unlink` then
`magent link`. ``link-state.json`` is not secret: the stream cursor, the ring
of command and upload ids already handled, and the state `magent status`
shows. A bad state file degrades to a fresh one.

The paths are bound at import, like every other ~/.magent path, so
tests/conftest.py's tripwire redirects them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from magent import fsutil
from magent.log import get_logger

LINK_PATH = Path.home() / ".magent" / "link.json"
LINK_STATE_PATH = Path.home() / ".magent" / "link-state.json"
INCOMING_DIR = Path.home() / ".magent" / "uploads" / ".incoming"

FILE_VERSION = 1
SEEN_MAX = 1024

_LINK_KEYS = ("relay_url", "host_id", "host_token", "host_kex_priv", "host_sign_priv")
_DEVICE_KEYS = ("device_id", "name", "kex_pub", "sign_pub", "paired_at")
_TOP_KEYS = frozenset(("version", "devices", *_LINK_KEYS))

# What _read_json returns for a file that is not JSON. The decode error itself
# is dropped, never chained: it carries the file's text, and link.json's text
# is the host token and both private keys.
_NOT_JSON = object()


class LinkFileError(ValueError):
    """link.json exists but is unsafe or not a magent link file."""


@dataclass(frozen=True)
class TrustedDevice:
    device_id: str
    name: str
    kex_pub: str
    sign_pub: str
    paired_at: str


@dataclass(frozen=True)
class LinkFile:
    relay_url: str
    host_id: str
    # The secrets stay out of repr(), so a log line or a traceback that
    # formats a LinkFile never prints the token or a key.
    host_token: str = field(repr=False)
    host_kex_priv: str = field(repr=False)
    host_sign_priv: str = field(repr=False)
    devices: tuple[TrustedDevice, ...]


@dataclass(frozen=True)
class LinkState:
    cursor: int = 0
    seen_ids: tuple[str, ...] = ()
    last_connected: float | None = None
    state: str = "unlinked"
    retry_at: float | None = None


def _link_path(path: Path | None) -> Path:
    return LINK_PATH if path is None else path


def _state_path(path: Path | None) -> Path:
    return LINK_STATE_PATH if path is None else path


def _is_current(raw: dict[str, object]) -> bool:
    """True when ``raw`` is FILE_VERSION exactly: an int, not True or 1.0."""
    version = raw.get("version")
    return type(version) is int and version == FILE_VERSION


def _read_json(p: Path) -> object:
    """``p`` parsed, or _NOT_JSON when it is not JSON. OSError propagates."""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        # Returned, not raised from in here: see _NOT_JSON.
        return _NOT_JSON


def _strings(
    raw: dict[str, object], keys: tuple[str, ...], where: Path
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in keys:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise LinkFileError(f"{where} is not a magent link file ({key})")
        out[key] = value
    return out


def load_link(path: Path | None = None) -> LinkFile | None:
    """The saved link, or None when this host is not linked."""
    p = _link_path(path)
    if not p.exists() and not p.is_symlink():
        return None
    try:
        fsutil.check_private(p)
    except fsutil.PrivateFileError as exc:
        raise LinkFileError(str(exc)) from exc
    try:
        raw = _read_json(p)
    except OSError as exc:
        raise LinkFileError(f"{p} is not a magent link file") from exc
    if raw is _NOT_JSON:
        raise LinkFileError(f"{p} is not a magent link file")
    if not isinstance(raw, dict) or not _is_current(raw):
        raise LinkFileError(f"{p} is not a magent link file (version)")
    if not set(raw) <= _TOP_KEYS:
        raise LinkFileError(f"{p} is not a magent link file (unexpected keys)")
    fields = _strings(raw, _LINK_KEYS, p)
    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list):
        raise LinkFileError(f"{p} is not a magent link file (devices)")
    devices: list[TrustedDevice] = []
    for item in devices_raw:
        if not isinstance(item, dict) or not set(item) <= set(_DEVICE_KEYS):
            raise LinkFileError(f"{p} is not a magent link file (devices)")
        devices.append(TrustedDevice(**_strings(item, _DEVICE_KEYS, p)))
    return LinkFile(devices=tuple(devices), **fields)


def save_link(link: LinkFile, path: Path | None = None) -> None:
    """Write link.json owner-only and atomically."""
    body = {"version": FILE_VERSION, **asdict(link)}
    body["devices"] = [asdict(d) for d in link.devices]
    fsutil.write_private(_link_path(path), json.dumps(body, indent=2).encode("utf-8"))


def delete_link(path: Path | None = None, state_path: Path | None = None) -> None:
    """Forget the link: both files, missing ones included."""
    _link_path(path).unlink(missing_ok=True)
    _state_path(state_path).unlink(missing_ok=True)


def load_state(path: Path | None = None) -> LinkState:
    """The saved state; a fresh LinkState when it is missing or unreadable."""
    p = _state_path(path)
    try:
        raw = _read_json(p)
    except FileNotFoundError:
        return LinkState()
    except OSError:
        raw = _NOT_JSON
    if not isinstance(raw, dict) or not _is_current(raw):
        get_logger("link").warning(
            "link: %s is unreadable; starting from a fresh state", p
        )
        return LinkState()
    cursor = raw.get("cursor")
    seen = raw.get("seen_ids")
    last = raw.get("last_connected")
    state = raw.get("state")
    retry = raw.get("retry_at")
    return LinkState(
        cursor=cursor
        if isinstance(cursor, int) and not isinstance(cursor, bool)
        else 0,
        seen_ids=tuple(s for s in seen if isinstance(s, str))[-SEEN_MAX:]
        if isinstance(seen, list)
        else (),
        last_connected=float(last)
        if isinstance(last, (int, float)) and not isinstance(last, bool)
        else None,
        state=state if isinstance(state, str) else "unlinked",
        retry_at=float(retry)
        if isinstance(retry, (int, float)) and not isinstance(retry, bool)
        else None,
    )


def save_state(state: LinkState, path: Path | None = None) -> None:
    """Write link-state.json atomically, keeping only the newest SEEN_MAX ids."""
    body = {
        "version": FILE_VERSION,
        "cursor": state.cursor,
        "seen_ids": list(state.seen_ids[-SEEN_MAX:]),
        "last_connected": state.last_connected,
        "state": state.state,
        "retry_at": state.retry_at,
    }
    fsutil.write_atomic(_state_path(path), json.dumps(body).encode("utf-8"))
