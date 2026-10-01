"""Shared builders for the placement and recall tests (PR-G).

One module, like tests/unit/_fake_ccswap.py, so test_node_place.py,
test_node_recall.py and test_node_cmd.py seed load histories, map entries and
transcripts the same way. Every path lands under the tmp home the autouse
conftest fixture set up: ``nodes.NODES_DIR`` is on ``_IMPORT_BOUND_PATHS``
(PR-B), so it is read at call time, never at import.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from magent import nodes
from magent.config import (
    SCHEMA_VERSION,
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "node_load"
# The instant the committed fixtures were written relative to: the newest
# sample of each sits exactly here, the window's oldest at NOW - 1800.
NOW = 1_790_000_000.0
SESSION_ID = "5f0c2a4e-8b1d-4c3e-9a7f-1234567890ab"
OLDER_SESSION_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def seed_history(
    nick: str, fixture: str, *, now: float = NOW, nodes_dir: Path | None = None
) -> Path:
    """Copy a committed load fixture to ``<nodes_dir>/<nick>/load.jsonl``,
    shifted so its reference instant lands on ``now``.

    Tests whose code under test reads the REAL clock seed with
    ``now=time.time() + 30``: the fixture's oldest in-window sample sits
    exactly on the window edge, so seeding at the bare ``time.time()`` would
    drop it out of the window a few milliseconds later (30 samples, not 31).
    The window has no upper bound, so a sample 30s in the future still counts.
    """
    target = nodes.load_path(nick, nodes_dir=nodes_dir)  # E's path helper (DECISION-19)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for line in (
        (FIXTURES / f"{fixture}.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        row = json.loads(line)
        row["ts"] = row["ts"] - NOW + now
        rows.append(json.dumps(row, separators=(",", ":")))
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return target


def pool(*nicks: str, projects: list[ProjectConfig] | None = None) -> MagentConfig:
    """A typed config whose settings.nodes are ``nicks``, in that order."""
    return MagentConfig(
        projects=projects or [],
        settings=Settings(
            nodes={n: NodeConfig(nick=n, host=f"box-{n}", user="demo") for n in nicks}
        ),
    )


def config_json(
    nicks: tuple[str, ...], projects: list[dict[str, object]]
) -> dict[str, object]:
    """The same pool as a config FILE body, for CLI tests (`tmp_config`)."""
    return {
        "version": SCHEMA_VERSION,
        "settings": {"nodes": {n: {"host": f"box-{n}", "user": "demo"} for n in nicks}},
        "projects": projects,
    }


def entry(nick: str, sid: str = "api") -> nodes.NodeMapEntry:
    """A node-map entry (B's type, DECISION-13) as D's bring-up writes it."""
    return nodes.NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=NOW,
        attached_existing=False,
        remote_root=f"~/magent/{sid}",
        target=f"demo@box-{nick}",
        cwd=f"/home/demo/magent/{sid}",
    )


def write_transcript(nick: str, sid: str, session_id: str, *, mtime: float) -> Path:
    """A pulled top-level transcript, shaped like the real ones (the stem is
    the session id; the records carry the NODE's cwd)."""
    folder = nodes.transcripts_dir(nick, sid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    record = {
        "type": "user",
        "sessionId": session_id,
        "cwd": f"/home/demo/magent/{sid}",
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def git(cwd: Path, *args: str) -> str:
    """Real git on a tmp repo only; identity and signing pinned so the
    developer's own config cannot change the outcome."""
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=magent-test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return done.stdout.strip()
