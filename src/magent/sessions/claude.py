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
# session named. Matched as a whole token (and with the trailing run of spaces,
# so removing one leaves no double space) rather than as a substring, so a
# longer flag that merely starts the same way is never touched.
#
# Long form ONLY, deliberately. claude also accepts `-c`, but a configured
# command may run claude through an interpreter -- `bash -c claude --continue`
# -- where that same token belongs to the WRAPPER, and stripping it would
# corrupt a working command. That is far worse than leaving the rarer
# `claude -c` spelling on its pre-existing behavior. The token lookahead
# handles the fully quoted wrapper payload (`bash -c "claude --continue"`) for
# free: the flag is followed by a quote, not whitespace, so nothing matches and
# the command is left exactly as the user wrote it.
_CONTINUE_RE = re.compile(r"(?:(?<=\s)|\A)--continue(?=\s|\Z)\s*")
# A session the user named explicitly (``--resume <id>``, ``--resume=<id>``,
# ``-r <id>``) or claude's interactive resume picker (a bare ``--resume``).
# Either way the command spells out what the user wants; the fresh-start
# rewrite below stays out of it.
_EXPLICIT_RESUME_RE = re.compile(r"(?:(?<=\s)|\A)(?:--resume|-r)(?=[\s=]|\Z)")


def default_config_dir() -> Path:
    """Claude Code's own config directory, ``~/.claude`` -- the store that
    answers for a project no account was chosen for.

    Resolved at CALL time and deliberately not a module constant: an
    import-bound ``Path.home()`` is computed once, before any environment
    redirect can reach it, which is precisely the defect class
    ``tests/conftest.py``'s ``_IMPORT_BOUND_PATHS`` tripwire exists to catch.
    """
    return Path.home() / ".claude"


def _projects_dir(config_dir: Path | None, project_dir: str) -> Path:
    """Where this store keeps ``project_dir``'s conversations.

    ``config_dir`` is a claude CONFIG directory -- the value of
    ``CLAUDE_CONFIG_DIR`` -- not a home directory: claude keeps its transcripts
    in ``<config dir>/projects/<encoded cwd>``, and under an account profile
    that config dir is the profile, not ``~``. None means the default store, so
    every caller that names no account reads exactly the path it always did.
    """
    root = config_dir if config_dir is not None else default_config_dir()
    return root / "projects" / encode_claude_project_path(project_dir)


def has_claude_session(project_dir: str, config_dir: Path | None = None) -> bool:
    """True when ``project_dir`` has at least one stored claude conversation
    IN ``config_dir``'s store (the default ``~/.claude`` one when None).

    Existence only -- first hit wins, no stat and no sort. This runs once per
    project on every status/attach sweep, so it must stay a directory peek
    rather than the full mtime-ordered listing ``get_claude_session_ids``
    builds. ``Path.glob`` over a directory that does not exist yields nothing
    instead of raising, which is exactly the "no sessions here" answer.

    Which store answers is a real question, not a test seam: a pane launched
    under ``CLAUDE_CONFIG_DIR=<profile>`` writes its transcripts there, so a
    probe that always read ``~/.claude`` would answer for a store that pane
    never touches -- dropping ``--continue`` from a project that does have a
    conversation, or keeping it for one that does not.
    """
    sess_dir = _projects_dir(config_dir, project_dir)
    return next(sess_dir.glob("*.jsonl"), None) is not None


def claude_fresh_command(
    base_cmd: str, project_dir: str, config_dir: Path | None = None
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
    conversation at all, in ``config_dir``'s store -- and only a NO rewrites
    anything. A session file that exists but is empty or corrupt counts as YES
    and keeps ``--continue``: that failure is a real defect the user needs to
    SEE in the pane, not something to paper over with a silently fresh chat.

    A project whose account changed has no transcript in the NEW store, so this
    answers NO and the agent starts fresh rather than dying on "No conversation
    found to continue" at a dead shell. That is the honest answer for that
    store; surfacing it to the user is the caller's job.
    """
    if _EXPLICIT_RESUME_RE.search(base_cmd) or not _CONTINUE_RE.search(base_cmd):
        return None
    if has_claude_session(project_dir, config_dir):
        return None
    return _CONTINUE_RE.sub("", base_cmd).strip()


def get_claude_session_ids(
    project_dir: str,
    count: int,
    config_dir: Path | None = None,
) -> list[str | None]:
    """``project_dir``'s stored conversation ids, newest first, out of
    ``config_dir``'s store (the default ``~/.claude`` one when None)."""
    sess_dir = _projects_dir(config_dir, project_dir)

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
