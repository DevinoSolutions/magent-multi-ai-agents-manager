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
