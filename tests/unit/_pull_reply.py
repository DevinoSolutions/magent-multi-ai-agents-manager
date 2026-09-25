"""Build a pull.sh reply the way a node prints one: the header line, one JSON
metadata line, then an uncompressed USTAR archive of ASCII files -- so the
whole reply survives the fake ssh's str-typed stdout byte-for-byte."""

from __future__ import annotations

import io
import json
import tarfile

from magent.remote_mux import PULL_HEADER

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


def pull_reply(meta: dict[str, object], files: dict[str, str] | None = None) -> str:
    out = io.BytesIO()
    out.write(PULL_HEADER)
    out.write(json.dumps(meta).encode("ascii") + b"\n")
    if files:
        with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, text in files.items():
                data = text.encode("ascii")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = MTIME
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
    return out.getvalue().decode("ascii")
