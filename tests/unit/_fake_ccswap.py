"""A real, on-disk fake ``ccswap`` binary for the account tests.

A structural mirror of ``_fake_psmux.py``, for the same reason: mocking
``subprocess`` proves the argv magent BUILDS, while running a genuine
executable proves the argv a process RECEIVES -- that it is a list and not a
shell line, that the JSON comes back over a single pipe, and that a hung child
is really bounded by the timeout rather than by a pipe nobody closed.

``make_fake_ccswap`` writes a tiny launcher (``.cmd`` on Windows, an ``sh``
script elsewhere) that shells to a Python recorder with its base directory
baked in as a literal, so no environment plumbing is needed. Every invocation
is recorded as its OWN file: ``_fake_psmux`` learned that the hard way, when
concurrent probes appending to one shared log tore a line on CI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_RECORDER = """\
import json, os, sys, time
from pathlib import Path

BASE = Path(r"{base}")
args = sys.argv[1:]

calldir = BASE / "calls"
calldir.mkdir(parents=True, exist_ok=True)
(calldir / (str(time.time_ns()) + "-" + str(os.getpid()) + ".json")).write_text(
    json.dumps(args), encoding="utf-8"
)

mode = (BASE / "mode.txt").read_text(encoding="utf-8").strip() if (BASE / "mode.txt").exists() else "ok"

if mode == "rc1":
    sys.stderr.write("ccswap: something went wrong\\n")
    sys.exit(1)

if mode == "garbage":
    # Real JSON is what the contract promises; this is what a half-installed
    # CLI prints instead (a node stack trace, an npm warning).
    sys.stdout.buffer.write(b"not json at all\\n")
    sys.exit(0)

if mode == "timeout":
    # Outlive any test's bound without ever writing to the pipe.
    time.sleep(120)
    sys.exit(0)

if "refresh" in args:
    sys.exit(0)

payload = (BASE / "payload.json").read_text(encoding="utf-8")
# Raw UTF-8 bytes: a bare sys.stdout.write() on Windows encodes to the console
# code page, which mangles any non-ASCII label -- the cp1252 defect the fleet
# tier hit. Real ccswap emits UTF-8.
sys.stdout.buffer.write(payload.encode("utf-8"))
sys.exit(0)
"""


def account(
    acct_id: str,
    *,
    label: str = "user@example.test",
    kind: str = "subscription",
    active: bool = False,
    profile_dir: str | None = None,
    hydrated: bool = True,
    eligible: bool = True,
    ineligible_reason: str | None = None,
    five_hour: float | None = 0.1,
    seven_day: float | None = 0.2,
    fable: float | None = None,
    five_hour_resets: str | None = "2026-09-21T09:30:00Z",
    seven_day_resets: str | None = "2026-09-27T09:30:00Z",
    overage_status: str = "rejected",
) -> dict:
    """One `accounts[]` entry in the C-1 shape. Utilizations are 0-1 floats or
    None, and None is emitted as JSON null -- never as 0."""
    usage: dict[str, object] = {
        "fiveHour": {"utilization": five_hour, "resetsAt": five_hour_resets},
        "sevenDay": {"utilization": seven_day, "resetsAt": seven_day_resets},
    }
    if fable is not None:
        usage["scoped"] = [
            {"scope": "fable", "utilization": fable, "resetsAt": seven_day_resets}
        ]
    return {
        "id": acct_id,
        "label": label,
        "provider": "claude",
        "kind": kind,
        "active": active,
        "profileDir": profile_dir or f"/ccswap/sessions/{acct_id}-profile",
        "profileHydrated": hydrated,
        "eligible": eligible,
        "ineligibleReason": ineligible_reason,
        "usage": usage,
        "overageStatus": overage_status,
    }


def payload(
    accounts: list[dict] | None = None,
    *,
    age_s: float | None = 660,
    fetched_at: str = "2026-09-21T03:40:00Z",
    settings: dict | None = None,
    duplicates: list[str] | None = None,
) -> dict:
    """A whole `ccswap list --json --provider claude --profiles` body."""
    body: dict[str, object] = {
        "usageCache": {"fetchedAt": fetched_at, "ageS": age_s},
        "duplicateAccountWarnings": duplicates or [],
        "accounts": accounts if accounts is not None else [account("13")],
    }
    if settings is not None:
        body["settings"] = settings
    return body


@dataclass
class FakeCcswap:
    """Handle onto a fake ccswap binary sitting on disk."""

    path: str
    base: Path

    def set_payload(self, body: dict) -> None:
        (self.base / "payload.json").write_text(json.dumps(body), encoding="utf-8")

    def set_accounts(self, accounts: list[dict], **kwargs) -> None:
        self.set_payload(payload(accounts, **kwargs))

    def set_mode(self, mode: str) -> None:
        """ "ok" | "rc1" | "garbage" | "timeout" -- every failure shape the
        seam promises to survive."""
        (self.base / "mode.txt").write_text(mode, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        d = self.base / "calls"
        if not d.exists():
            return []
        files = sorted(d.glob("*.json"), key=lambda p: int(p.name.split("-")[0]))
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def make_fake_ccswap(tmp_path: Path, *, body: dict | None = None) -> FakeCcswap:
    base = tmp_path / "fakeccswap"
    base.mkdir(parents=True, exist_ok=True)
    (base / "recorder.py").write_text(
        _RECORDER.format(base=str(base)), encoding="utf-8"
    )
    (base / "payload.json").write_text(
        json.dumps(body if body is not None else payload()), encoding="utf-8"
    )

    if sys.platform == "win32":
        launcher = base / "ccswap.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{base / "recorder.py"}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = base / "ccswap"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{base / "recorder.py"}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return FakeCcswap(path=str(launcher), base=base)
