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


# "Pick the current directory's most recent conversation back up", with no
# session named. Matched as whole tokens (and with the trailing run of spaces,
# so removing one leaves no double space) rather than as a substring, so a
# longer flag that merely starts the same way is never touched.
_CONTINUE_RE = re.compile(r"(?:(?<=\s)|\A)(?:--continue|-c)(?=\s|\Z)\s*")
# A session the user named explicitly (``--resume <id>``, ``--resume=<id>``,
# ``-r <id>``) or claude's interactive resume picker (a bare ``--resume``).
# Either way the command spells out what the user wants; the fresh-start
# rewrite below stays out of it.
_EXPLICIT_RESUME_RE = re.compile(r"(?:(?<=\s)|\A)(?:--resume|-r)(?=[\s=]|\Z)")


def has_claude_session(project_dir: str, home_override: Path | None = None) -> bool:
    """True when ``project_dir`` has at least one stored claude conversation.

    Existence only -- first hit wins, no stat and no sort. This runs once per
    project on every status/attach sweep, so it must stay a directory peek
    rather than the full mtime-ordered listing ``get_claude_session_ids``
    builds. ``Path.glob`` over a directory that does not exist yields nothing
    instead of raising, which is exactly the "no sessions here" answer.
    """
    home = home_override or Path.home()
    sess_dir = home / ".claude" / "projects" / encode_claude_project_path(project_dir)
    return next(sess_dir.glob("*.jsonl"), None) is not None


def claude_fresh_command(
    base_cmd: str, project_dir: str, home_override: Path | None = None
) -> str | None:
    """``base_cmd`` minus its implicit-resume flag when ``project_dir`` has no
    conversation to resume -- or None to run ``base_cmd`` exactly as configured.

    ``claude --continue`` (the registry default) resumes the most recent
    conversation *for the current working directory*. In a directory that never
    hosted one -- a project just added to magent, a fresh machine, a cleaned
    ``~/.claude/projects`` -- there is nothing to continue: claude prints "No
    conversation found to continue" and exits, so the pane is left at a dead
    shell, the agent never starts, and revive re-runs the same failing command
    forever. Dropping the flag is the honest repair: what the user asked for
    was an agent in that folder.

    The probe answers exactly one question -- does this directory have a stored
    conversation at all -- and only a NO rewrites anything. A session file that
    exists but is empty or corrupt counts as YES and keeps ``--continue``: that
    failure is a real defect the user needs to SEE in the pane, not something
    to paper over with a silently fresh chat.
    """
    if _EXPLICIT_RESUME_RE.search(base_cmd) or not _CONTINUE_RE.search(base_cmd):
        return None
    if has_claude_session(project_dir, home_override):
        return None
    return _CONTINUE_RE.sub("", base_cmd).strip()


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
