from __future__ import annotations

import json
import sys
from pathlib import Path


def build_codex_resume(base_cmd: str, session_id: str | None) -> str:
    parts = base_cmd.split(None, 1)
    binary = parts[0]
    if session_id:
        return f"{binary} resume {session_id}"
    return base_cmd


def codex_fresh_command(
    base_cmd: str, project_dir: str, home_override: Path | None = None
) -> str | None:
    """The codex twin of ``claude_fresh_command`` -- almost always None.

    codex resumes through the explicit subcommand ``codex resume <id>``, which
    magent only ever builds when it HAS an id, and the registry default is a
    bare ``codex``. So codex has no equivalent of ``claude --continue``'s
    "resume whatever was last here" default and, for every command this repo
    generates, there is nothing to rewrite.

    The one hand-configured shape that carries the same hazard is
    ``codex resume --last``: in a directory codex has never run in there is no
    last session to resume. That form (and only that form) drops back to the
    bare binary. ``resume <id>`` is the user naming a session explicitly and is
    left alone, exactly like claude's ``--resume <id>``.
    """
    tokens = base_cmd.split()
    if "resume" not in tokens:
        return None
    at = tokens.index("resume")
    rest = tokens[at + 1 :]
    if rest[:1] != ["--last"]:
        return None
    if get_codex_session_ids(project_dir, 1, home_override)[0] is not None:
        return None
    return " ".join(tokens[:at] + rest[1:])


def get_codex_session_ids(
    project_dir: str,
    count: int,
    home_override: Path | None = None,
) -> list[str | None]:
    home = home_override or Path.home()
    sess_root = home / ".codex" / "sessions"

    if not sess_root.is_dir():
        return [None] * count

    case_insensitive = sys.platform == "win32"
    compare_dir = project_dir.lower() if case_insensitive else project_dir

    files = sorted(
        sess_root.rglob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    ids: list[str | None] = []
    for f in files:
        if len(ids) >= count:
            break
        try:
            with open(f, encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
            cwd = meta.get("payload", {}).get("cwd", "")
            if case_insensitive:
                cwd = cwd.lower()
            if cwd == compare_dir:
                ids.append(meta["payload"]["id"])
        except (json.JSONDecodeError, KeyError, OSError):
            continue

    while len(ids) < count:
        ids.append(None)
    return ids
