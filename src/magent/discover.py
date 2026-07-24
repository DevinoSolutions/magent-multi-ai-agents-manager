from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

from magent.config import _derive_tab_color, default_config
from magent.sessions.claude import encode_claude_project_path


def _field_str(d: dict[str, object], key: str) -> str:
    """A descriptor dict's string field, narrowed from dict[str, object]."""
    value = d.get(key, "")
    return value if isinstance(value, str) else ""


def _field_int(d: dict[str, object], key: str) -> int:
    value = d.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _field_mtime(d: dict[str, object]) -> float:
    """The 'last_active' epoch seconds of a descriptor, or 0.0."""
    value = d.get("last_active", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return value


def _claude_sessions_for_path(
    project_path: str, home: Path | None = None
) -> dict[str, object] | None:
    """Check if Claude has sessions for a given project path."""
    home = home or Path.home()
    encoded = encode_claude_project_path(project_path)
    sess_dir = home / ".claude" / "projects" / encoded
    if not sess_dir.is_dir():
        return None
    sessions = list(sess_dir.glob("*.jsonl"))
    if not sessions:
        return None
    return {
        "session_count": len(sessions),
        "last_active": max(f.stat().st_mtime for f in sessions),
    }


def _discover_codex_projects(home: Path | None = None) -> list[dict[str, object]]:
    home = home or Path.home()
    sess_root = home / ".codex" / "sessions"
    if not sess_root.is_dir():
        return []

    seen: dict[str, dict[str, object]] = {}
    for f in sess_root.rglob("*.jsonl"):
        try:
            with open(f, encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
            cwd = meta.get("payload", {}).get("cwd", "")
            if not cwd or not Path(cwd).is_dir():
                continue
            mtime = f.stat().st_mtime
            key = cwd.lower() if sys.platform == "win32" else cwd
            prev = seen.get(key)
            seen[key] = {
                "path": cwd,
                "tool": "codex",
                "session_count": (_field_int(prev, "session_count") if prev else 0) + 1,
                "last_active": max(mtime, _field_mtime(prev) if prev else 0.0),
            }
        except (json.JSONDecodeError, KeyError, OSError):
            continue

    return list(seen.values())


def _vscode_storage_dir() -> Path | None:
    from magent.env import vscode_storage_base  # heavy subsystem: in-body per policy

    d = vscode_storage_base() / "Code" / "User" / "workspaceStorage"
    return d if d.is_dir() else None


def _uri_to_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if sys.platform == "win32" and path.startswith("/"):
        path = path[1:]
    return path


def _discover_vscode_projects() -> list[dict[str, object]]:
    storage = _vscode_storage_dir()
    if not storage:
        return []

    results: list[dict[str, object]] = []
    for d in storage.iterdir():
        wj = d / "workspace.json"
        if not wj.is_file():
            continue
        try:
            data = json.loads(wj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        folder_uri = data.get("folder", "")
        if not folder_uri:
            continue
        folder = _uri_to_path(folder_uri)
        if not folder or not Path(folder).is_dir():
            continue
        mtime = d.stat().st_mtime
        results.append(
            {
                "path": folder,
                "tool": "vscode",
                "session_count": 1,
                "last_active": mtime,
            }
        )

    return results


def _discover_claude_standalone(
    home: Path | None = None, known_encoded: set[str] | None = None
) -> list[dict[str, object]]:
    """Discover Claude projects that weren't found via Codex (brute-force decode)."""
    home = home or Path.home()
    projects_dir = home / ".claude" / "projects"
    if not projects_dir.is_dir():
        return []

    known_encoded = known_encoded or set()
    results: list[dict[str, object]] = []

    for d in projects_dir.iterdir():
        if not d.is_dir() or d.name in known_encoded:
            continue
        sessions = list(d.glob("*.jsonl"))
        if not sessions:
            continue
        decoded = _try_decode(d.name)
        if not decoded:
            continue
        results.append(
            {
                "path": decoded,
                "tool": "claude",
                "session_count": len(sessions),
                "last_active": max(f.stat().st_mtime for f in sessions),
            }
        )

    return results


def _try_decode(encoded: str) -> str | None:
    """Best-effort decode of a Claude project directory name."""
    if sys.platform == "win32":
        if not encoded.startswith("C--"):
            return None
        rest = encoded[3:]
        candidate = "C:\\" + rest.replace("-", "\\")
        if Path(candidate).is_dir():
            return candidate
    else:
        if not encoded.startswith("-"):
            return None
        candidate = "/" + encoded[1:].replace("-", "/")
        if Path(candidate).is_dir():
            return candidate
    return None


GENERIC_DIRS = {
    "desktop",
    "documents",
    "downloads",
    "projects",
    "repos",
    "src",
    "home",
    "work",
}


def _is_real_project(path: str) -> bool:
    p = Path(path)
    parts = p.parts
    min_depth = 5 if sys.platform == "win32" else 4
    if len(parts) < min_depth:
        return False
    return p.name.lower() not in GENERIC_DIRS


STEP_DAYS = 3
MIN_PROJECTS = 3
MAX_STEPS = 160


def _merge_candidate(
    by_path: dict[str, dict[str, object]], key: str, cand: dict[str, object]
) -> None:
    """Offer one candidate for `key`, keeping whichever has the max
    last_active seen so far. Ties go to whichever was offered first (R9)."""
    if key not in by_path or _field_mtime(cand) > _field_mtime(by_path[key]):
        by_path[key] = cand


def discover_projects(home: Path | None = None) -> tuple[list[dict[str, object]], int]:
    """Find projects from Claude and Codex session history.

    Expands the time window (3 days, 6, 9, ...) until at least
    MIN_PROJECTS are found or MAX_STEPS is reached.

    Returns (projects, days_searched).
    """
    import time

    home = home or Path.home()
    codex = _discover_codex_projects(home)
    vscode = _discover_vscode_projects()

    by_path: dict[str, dict[str, object]] = {}
    matched_encoded: set[str] = set()

    def _norm_key(path: str) -> str:
        k = os.path.normpath(path)
        return k.lower() if sys.platform == "win32" else k

    for p in codex + vscode:
        path = _field_str(p, "path")
        if not _is_real_project(path):
            continue
        path = os.path.normpath(path)
        p["path"] = path
        key = _norm_key(path)

        claude_info = _claude_sessions_for_path(path, home)
        if claude_info:
            encoded = encode_claude_project_path(path)
            matched_encoded.add(encoded)
            _merge_candidate(
                by_path,
                key,
                {
                    "path": path,
                    "tool": "claude",
                    "session_count": claude_info["session_count"],
                    "last_active": claude_info["last_active"],
                },
            )

        _merge_candidate(by_path, key, p)

    for p in _discover_claude_standalone(home, matched_encoded):
        path = _field_str(p, "path")
        if not _is_real_project(path):
            continue
        path = os.path.normpath(path)
        p["path"] = path
        key = _norm_key(path)
        _merge_candidate(by_path, key, p)

    all_projects = sorted(by_path.values(), key=_field_mtime, reverse=True)
    if not all_projects:
        return [], 0

    now = time.time()
    for step in range(1, MAX_STEPS + 1):
        days = step * STEP_DAYS
        cutoff = now - (days * 86400)
        recent = [p for p in all_projects if _field_mtime(p) >= cutoff]
        if len(recent) >= MIN_PROJECTS:
            return recent, days

    return all_projects, MAX_STEPS * STEP_DAYS


def _find_base_dir(paths: list[str]) -> str:
    """Find the deepest directory shared by at least 60% of projects."""
    if not paths:
        return ""
    threshold = max(3, int(len(paths) * 0.6))
    candidate = os.path.commonpath(paths)

    while True:
        children: dict[str, int] = {}
        for p in paths:
            try:
                rel = os.path.relpath(p, candidate)
            except ValueError:
                continue
            first = rel.split(os.sep)[0]
            if first and first != ".":
                children[first] = children.get(first, 0) + 1

        best = max(children, key=lambda k: children[k]) if children else None
        if best and children[best] >= threshold:
            candidate = os.path.join(candidate, best)
        else:
            break

    return candidate


def projects_to_config(projects: list[dict[str, object]]) -> dict[str, object]:
    paths = [_field_str(p, "path") for p in projects]
    base_dir = _find_base_dir(paths) if projects else ""

    leaf_counts = Counter(Path(_field_str(p, "path")).name for p in projects)
    dup_leaves = {name for name, count in leaf_counts.items() if count > 1}

    used: set[str] = set()
    config_projects: list[dict[str, object]] = []
    for p in projects:
        path = _field_str(p, "path")
        try:
            rel = os.path.relpath(path, base_dir).replace("\\", "/")
        except ValueError:
            rel = path.replace("\\", "/")

        entry: dict[str, object] = {"path": rel}

        parent = Path(path).parent.name.lower()
        if (
            parent
            and parent not in GENERIC_DIRS
            and parent != Path(base_dir).name.lower()
        ):
            entry["group"] = Path(path).parent.name

        if (parts := rel.split("/")) and parts[-1] in dup_leaves:
            entry["title"] = rel.replace("/", "-")

        if p["tool"] != "claude":
            entry["tool"] = p["tool"]
        color = _derive_tab_color(str(entry.get("title") or entry["path"]), used)
        used.add(color)
        entry["color"] = color
        config_projects.append(entry)

    return default_config(config_projects, base_dir=base_dir)
