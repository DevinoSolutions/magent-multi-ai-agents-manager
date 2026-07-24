from __future__ import annotations

import re
from pathlib import Path


def encode_claude_project_path(project_dir: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", project_dir)


def build_claude_resume(base_cmd: str, session_id: str | None) -> str:
    stripped = re.sub(r"--continue\s*", "", base_cmd)
    stripped = re.sub(r"--resume\s+\S+", "", stripped).strip()
    if session_id:
        return f"{stripped} --resume {session_id}"
    return stripped


def get_claude_session_ids(
    project_dir: str,
    count: int,
    home_override: Path | None = None,
) -> list[str | None]:
    encoded = encode_claude_project_path(project_dir)
    home = home_override or Path.home()
    sess_dir = home / ".claude" / "projects" / encoded

    if not sess_dir.is_dir():
        return [None] * count

    files = sorted(
        sess_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    ids: list[str | None] = [f.stem for f in files[:count]]
    while len(ids) < count:
        ids.append(None)
    return ids
