"""`magent hooks` -- wire agent lifecycle hooks into Claude Code so the
agent-state store (read by `sessions`, `watch`, and the attention daemon)
actually gets fed. `install` merges idempotently into ~/.claude/settings.json
and prints the Codex recipe; `status` reports what is wired and how fresh the
store is.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import click

from magent.cli.app import main
from magent.style import style

_EVENTS: tuple[str, ...] = (
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "Notification",
    "SessionStart",
    "SessionEnd",
)

# Substring that identifies our entries inside settings.json -- the console
# script's name, present in any command string that invokes it.
_MARKER = "magent-state-hook"
# The same writer run as a module (`<python> -m magent.state_hook`), the
# hand-wired spelling that survives a pip rollback deleting the console script.
_MODULE_MARKER = "-m magent.state_hook"
# A Windows drive-letter path (`C:\`): the only module-form backslashes a repair
# swaps. Any other backslash is taken as bash escape syntax (`My\ Venv`), so a
# UNC (`\\host\share`) or mixed-separator (`C:/py\python.exe`) interpreter path
# is knowingly left alone -- it reads as wired though bash cannot run it.
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\")


def _is_ours(text: str) -> bool:
    return _MARKER in text or _MODULE_MARKER in text


def _default_settings_file() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _hook_exe() -> str:
    """Absolute path to the state-hook console script, in forward-slash form.
    Resolved at install time: hooks run with whatever PATH the host session
    has, which need not include this install's Scripts dir. Forward slashes
    because Claude Code executes hook commands through a POSIX shell even on
    Windows, where a raw backslash path is eaten as escape sequences -- and
    every Windows API accepts the forward-slash spelling anyway. Plain
    replace, not Path.as_posix(): a PosixPath doesn't treat backslash as a
    separator, so as_posix() would pass a Windows path through unchanged."""
    exe = shutil.which(_MARKER) or _MARKER
    return exe.replace("\\", "/")


def _hook_command() -> str:
    """The shell command Claude Code should run per event."""
    exe = _hook_exe()
    quoted = f'"{exe}"' if " " in exe else exe
    return f"{quoted} --source claude"


def _codex_recipe() -> str:
    return f'notify = [{json.dumps(_hook_exe())}, "--source", "codex"]'


def _load_settings(path: Path) -> dict[str, object] | str:
    """The parsed settings.json ({} when there is none), or what is wrong with it.

    The string, in our words plus the error's class, is for a file magent
    cannot safely understand: unreadable, not UTF-8, not JSON, nested past the
    parser's depth, or not the shape Claude Code writes -- ``hooks`` an
    object, each of our events' values an array. install refuses such a file
    untouched (the wt_keys law: a file magent cannot read is never rewritten;
    before this, a wrong-shaped ``hooks`` was silently replaced), and status
    says so instead of reporting every event unwired.

    Returned, never raised: settings.json can hold API keys in its "env"
    block, and an exception keeps alive both a parser error (a
    JSONDecodeError's ``.doc`` is the whole file) and the frame that parsed
    it, whose locals hold the file.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        problem = f"not valid UTF-8 ({type(exc).__name__})"
    except ValueError as exc:
        problem = f"not valid JSON ({type(exc).__name__})"
    except RecursionError as exc:
        problem = f"nested too deeply to parse ({type(exc).__name__})"
    except OSError as exc:
        problem = f"could not be read ({type(exc).__name__})"
    else:
        if not isinstance(data, dict):
            return "not a JSON object"
        if not isinstance(hooks := data.get("hooks", {}), dict):
            return '"hooks" is not a JSON object'
        wrong = [e for e in _EVENTS if not isinstance(hooks.get(e, []), list)]
        if wrong:
            return f'"hooks.{wrong[0]}" is not a JSON array'
        return data
    return problem


def _event_wired(entries: object) -> bool:
    return isinstance(entries, list) and any(_is_ours(json.dumps(e)) for e in entries)


def _repair_entries(entries: list[object], cmd: str) -> bool:
    """Rewrite any wired magent-state-hook command that bash cannot run -- a
    backslash path from a pre-3.1.2 install, or a module-form one with a
    drive-letter backslash interpreter path (idempotence would otherwise skip
    the broken entry forever). Returns True when something was rewritten.

    A module-form command keeps its form: that spelling exists to avoid the
    console script, so only the backslashes bash would eat are swapped -- and
    only when it carries a Windows drive-letter path. Any other module-form
    backslash is taken as escape syntax and left byte for byte, which knowingly
    misses a UNC or mixed-separator interpreter path (see _DRIVE_PATH)."""
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for h in hooks:
            if not isinstance(h, dict):
                continue
            c = h.get("command")
            if not isinstance(c, str) or "\\" not in c:
                continue
            if _MODULE_MARKER in c:
                if _DRIVE_PATH.search(c):
                    h["command"] = c.replace("\\", "/")
                    changed = True
            elif _MARKER in c:
                h["command"] = cmd
                changed = True
    return changed


def _wire_settings(path: Path) -> tuple[list[str], list[str]] | str:
    """Merge one magent-state-hook entry per event into the settings.json at
    ``path`` and write it back: the events (added, repaired), or why the file
    cannot be edited.

    Returned, never raised, for _load_settings's reason: the command raises
    SystemExit, and the traceback keeps that frame's locals alive -- so the
    parsed file, API keys and all, lives only in this one.
    """
    data = _load_settings(path)
    if isinstance(data, str):
        return data

    # _load_settings refused any other shape: these defaults only fill in what
    # is absent.
    hooks_raw = data.get("hooks")
    hooks: dict[str, object] = hooks_raw if isinstance(hooks_raw, dict) else {}
    data["hooks"] = hooks
    cmd = _hook_command()
    added: list[str] = []
    repaired: list[str] = []
    for event in _EVENTS:
        entries_raw = hooks.get(event)
        entries: list[object] = entries_raw if isinstance(entries_raw, list) else []
        hooks[event] = entries
        if _event_wired(entries):
            if _repair_entries(entries, cmd):
                repaired.append(event)
            continue
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": cmd, "timeout": 10}]
        }
        if event == "PostToolUse":
            entry = {"matcher": "*", **entry}
        entries.append(entry)
        added.append(event)

    problem = _write_settings(path, data)
    return (added, repaired) if problem is None else problem


def _write_settings(path: Path, data: dict[str, object]) -> str | None:
    """Replace the settings.json at ``path`` with ``data``: None, or what
    stopped the write in our words plus the error's class. A failed write
    removes its temp file (best effort), leaving nothing beside the file.

    The file itself is opened for writing first: POSIX renames over a
    read-only file as freely as over any other (a rename is the directory's
    business), so the replace alone would overwrite a settings.json the user
    made read-only -- and hand it back writable.
    """
    tmp = path.with_suffix(".tmp")
    try:
        if path.exists():
            with path.open("r+b"):
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return f"could not be written ({type(exc).__name__})"
    return None


@main.group("hooks")
def hooks_group() -> None:
    """Wire agent lifecycle hooks that feed the session-state store."""


@hooks_group.command("install")
@click.option(
    "--settings-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Claude Code settings.json to edit (default: ~/.claude/settings.json).",
)
def hooks_install_cmd(settings_file: Path | None) -> None:
    """Add magent's state hook to Claude Code so session states stay accurate.

    Merges one magent-state-hook entry per lifecycle event into settings.json
    (idempotent; existing hooks are preserved). Prints the Codex notify recipe
    -- ~/.codex/config.toml is TOML, edited by hand.
    """
    path = settings_file or _default_settings_file()
    wired = _wire_settings(path)
    if isinstance(wired, str):
        click.echo(f"  {style('x', fg='red')} Cannot edit {path}: {wired}", err=True)
        raise SystemExit(1)

    added, repaired = wired
    if added:
        click.echo(
            f"  {style('+', fg='green', bold=True)} Wired {', '.join(added)} in {style(str(path), dim=True)}"
        )
    if repaired:
        click.echo(
            f"  {style('+', fg='green', bold=True)} Repaired stale hook command for "
            f"{', '.join(repaired)} {style('(Windows backslash path)', dim=True)}"
        )
    if added or repaired:
        click.echo(
            f"  {style('Restart open Claude Code sessions to pick the hooks up.', dim=True)}"
        )
    else:
        click.echo(
            f"  {style('=', fg='green', bold=True)} Already wired in {style(str(path), dim=True)}"
        )
    click.echo()
    click.echo(f"  {style('Codex:', bold=True)} add to ~/.codex/config.toml:")
    click.echo(f"    {style(_codex_recipe(), fg='cyan')}")


@hooks_group.command("status")
@click.option(
    "--settings-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Claude Code settings.json to inspect (default: ~/.claude/settings.json).",
)
def hooks_status_cmd(settings_file: Path | None) -> None:
    """Show which lifecycle hooks are wired and how fresh the state store is."""
    from magent import agent_state  # heavy subsystem: in-body per policy

    path = settings_file or _default_settings_file()
    data = _load_settings(path)
    if isinstance(data, str):
        # Never per-event rows here: "x" would claim every event is unwired,
        # which a file magent cannot read says nothing about.
        click.echo(
            f"  {style('x', fg='red')} {path}: {data}; cannot tell which hooks "
            "are wired",
            err=True,
        )
    else:
        hooks = data.get("hooks")
        hooks_map = hooks if isinstance(hooks, dict) else {}
        for event in _EVENTS:
            wired = _event_wired(hooks_map.get(event))
            mark = style("+", fg="green", bold=True) if wired else style("x", fg="red")
            click.echo(f"  {mark} {event}")
    click.echo()
    _echo_store_freshness(agent_state.all_states())
    if isinstance(data, str):
        # Unknown is not success: a script must not read "cannot tell" as
        # "all wired". The store report above still ran.
        raise SystemExit(1)


def _echo_store_freshness(records: list[dict[str, object]]) -> None:
    if not records:
        click.echo(
            f"  {style('State store is empty', fg='yellow')} "
            f"{style('-- run magent hooks install, then start an agent turn.', dim=True)}"
        )
        return
    newest = 0.0
    for rec in records:
        ts = rec.get("ts", 0)
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            newest = max(newest, float(ts))
    age_min = int(max(0.0, time.time() - newest) // 60)
    click.echo(
        f"  {style(str(len(records)), bold=True)} state record(s), "
        f"newest {style(f'{age_min}m ago', fg='cyan')}"
    )
