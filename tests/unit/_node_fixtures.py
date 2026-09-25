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

import pytest

from magent import launch, nodes, remote_mux
from magent.config import (
    SCHEMA_VERSION,
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)

# D-MERGE: THE one list of sub-plan D attributes that G's deferred code needs
# (the index at the top of src/magent/cli/node_cmd.py says what lands with
# each). Every G test that waits on D is gated through needs_d / before_d
# below -- never a hand-rolled hasattr -- so D's merge flips them all at once.
# Exit criterion: after D merges,
#   uv run pytest tests/unit/test_node_cmd.py tests/unit/test_node_recall.py -rs
# shows no skip reason containing "D-MERGE", and
#   git grep -n D-MERGE -- src tests
# comes back empty (this list and its two helpers go with the last of them).
D_ATTRS: dict[str, object] = {
    "node_recipe": launch,
    "node_git_states": launch,
    "bring_up_node_project": launch,
    "NodeBringUpOutcome": launch,
    "push_files": remote_mux,
    "kill_session": remote_mux,
}
D_LANDED: frozenset[str] = frozenset(
    name for name, module in D_ATTRS.items() if hasattr(module, name)
)


def _known(names: tuple[str, ...]) -> None:
    unknown = sorted(set(names) - D_ATTRS.keys())
    if unknown:
        raise KeyError(f"not in D_ATTRS: {unknown} -- add them to the one list")


def needs_d(*names: str, plan: str) -> pytest.MarkDecorator:
    """Skip until every one of ``names`` (all in D_ATTRS) is on this branch;
    ``plan`` is the plan-G line range of the code that lands with them."""
    _known(names)
    return pytest.mark.skipif(
        not D_LANDED.issuperset(names),
        reason=f"D-MERGE: needs D's {', '.join(names)} (plan G {plan})",
    )


def before_d(*names: str) -> pytest.MarkDecorator:
    """A pre-D pin: it runs only while one of ``names`` is missing. Once they
    all land it skips with a D-MERGE reason, so the exit criterion above
    catches a pin nobody deleted."""
    _known(names)
    return pytest.mark.skipif(
        D_LANDED.issuperset(names),
        reason=f"D-MERGE: pre-D pin -- delete it now that {', '.join(names)} landed",
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
            nodes={
                n: NodeConfig(nick=n, host=f"devino-{n}", user="amin") for n in nicks
            }
        ),
    )


def config_json(
    nicks: tuple[str, ...], projects: list[dict[str, object]]
) -> dict[str, object]:
    """The same pool as a config FILE body, for CLI tests (`tmp_config`)."""
    return {
        "version": SCHEMA_VERSION,
        "settings": {
            "nodes": {n: {"host": f"devino-{n}", "user": "amin"} for n in nicks}
        },
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
        target=f"amin@devino-{nick}",
        cwd=f"/home/amin/magent/{sid}",
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
        "cwd": f"/home/amin/magent/{sid}",
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
