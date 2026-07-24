from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from magent.config import _derive_tab_color, default_config

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".svn",
    ".hg",
    "bin",
    "obj",
    ".next",
    "dist",
    "vendor",
    ".venv",
    "target",
}


def scan_for_projects(
    root: str, max_depth: int = 3, *, skipped: list[str] | None = None
) -> list[dict[str, object]]:
    """Walk ``root`` for git repos. Directories denied by a ``PermissionError``
    are skipped; when a ``skipped`` list is passed, their paths are recorded so
    the caller can report how many were omitted (F-OB-005 / P2-06)."""
    root_path = Path(root).resolve()
    repos: list[Path] = []
    stack: list[tuple[Path, int]] = [(root_path, 0)]

    while stack and len(repos) < 300:
        current, depth = stack.pop()
        if depth >= 1 and (current / ".git").is_dir():
            repos.append(current)
            continue
        if depth < max_depth:
            try:
                children = sorted(current.iterdir())
                for child in children:
                    if child.is_dir() and child.name not in SKIP_DIRS:
                        stack.append((child, depth + 1))
            except PermissionError:
                if skipped is not None:
                    skipped.append(str(current))
                continue

    dirs = (
        sorted(repos)
        if repos
        else sorted(
            d for d in root_path.iterdir() if d.is_dir() and d.name not in SKIP_DIRS
        )
    )

    leaves = [d.name for d in dirs]
    leaf_counts = Counter(leaves)
    dup_leaves = {name for name, count in leaf_counts.items() if count > 1}

    used: set[str] = set()
    projects: list[dict[str, object]] = []
    for d in dirs:
        rel = d.relative_to(root_path).as_posix()
        parts = rel.split("/")
        proj: dict[str, object] = {"path": rel}
        if len(parts) > 1:
            proj["group"] = parts[0]
        if parts[-1] in dup_leaves:
            proj["title"] = rel.replace("/", "-")
        color = _derive_tab_color(str(proj.get("title") or proj["path"]), used)
        used.add(color)
        proj["color"] = color
        projects.append(proj)

    return projects


def generate_config(
    root: str, *, skipped: list[str] | None = None
) -> dict[str, object]:
    projects = scan_for_projects(root, skipped=skipped)
    return default_config(projects, base_dir=str(Path(root).resolve()))


def write_config(root: str, out_path: str, force: bool = False) -> tuple[bool, int]:
    """Write a generated config. Returns ``(wrote, skipped_count)`` -- the count
    of directories skipped because they were unreadable during the scan, so the
    CLI can surface them (P2-06). ``skipped_count`` is 0 when nothing was
    written (file already exists and ``force`` is False)."""
    out = Path(out_path)
    if out.exists() and not force:
        return False, 0
    skipped: list[str] = []
    config = generate_config(root, skipped=skipped)
    out.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return True, len(skipped)
