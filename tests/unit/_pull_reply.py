"""Build a pull.sh reply the way a node prints one: the header line, one JSON
metadata line, an uncompressed archive, then the ``PULL_TRAILER`` line with
the member count. ``pull_reply`` keeps to USTAR and ASCII files, so the whole
reply survives the fake ssh's str-typed stdout byte-for-byte; ``pull_bytes``
is the raw builder for the replies a node should never send."""

from __future__ import annotations

import io
import json
import tarfile
from typing import TYPE_CHECKING

from magent.remote_mux import PULL_HEADER, PULL_TRAILER

if TYPE_CHECKING:
    from collections.abc import Sequence

SAMPLE = {
    "ts": 4990,
    "nproc": 8,
    "load1": 0.5,
    "load5": 0.4,
    "load15": 0.3,
    "mem_total_mb": 16000,
    "mem_avail_mb": 12000,
    "my_sessions": 2,
}
MTIME = 4000


def pull_meta(**over: object) -> dict[str, object]:
    meta: dict[str, object] = {
        "now": 5000.0,
        "sessions": [],
        "sample": SAMPLE,
        "realpaths": {},
        "state_files": {},
    }
    meta.update(over)
    return meta


def member(
    name: str,
    data: bytes = b"x",
    *,
    mtime: float = MTIME,
    pax: dict[str, str] | None = None,
) -> tuple[tarfile.TarInfo, bytes]:
    """One regular-file member. ``pax`` overrides header fields the way a
    PAX writer does (``{"mtime": "nan"}``); it needs ``PAX_FORMAT``."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o600
    if pax is not None:
        info.pax_headers = dict(pax)
    return info, data


def pull_bytes(
    meta: dict[str, object],
    members: Sequence[tuple[tarfile.TarInfo, bytes]] = (),
    *,
    fmt: int = tarfile.USTAR_FORMAT,
    compression: str = "",
    count: int | None = None,
) -> bytes:
    """A reply as bytes. ``count`` is what the trailer claims (default: the
    real member count); an empty ``members`` writes no archive at all."""
    out = io.BytesIO()
    out.write(PULL_HEADER)
    out.write(json.dumps(meta).encode("ascii") + b"\n")
    if members:
        with tarfile.open(fileobj=out, mode=f"w:{compression}", format=fmt) as tar:
            for info, data in members:
                tar.addfile(info, io.BytesIO(data))
    claimed = len(members) if count is None else count
    out.write(PULL_TRAILER + str(claimed).encode("ascii") + b"\n")
    return out.getvalue()


def archive_start(reply: bytes) -> int:
    """Where the archive begins: just after the metadata line."""
    return reply.index(b"\n", reply.index(PULL_HEADER) + len(PULL_HEADER)) + 1


def pull_reply(meta: dict[str, object], files: dict[str, str] | None = None) -> str:
    members = [
        member(name, text.encode("ascii")) for name, text in (files or {}).items()
    ]
    return pull_bytes(meta, members).decode("ascii")
