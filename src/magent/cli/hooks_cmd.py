"""`magent hooks` -- wire agent lifecycle hooks into Claude Code so the
agent-state store (read by `sessions`, `watch`, and the attention daemon)
actually gets fed. `install` merges idempotently into ~/.claude/settings.json
and prints the Codex recipe; `status` reports what is wired and how fresh the
store is.
"""

from __future__ import annotations

import json
import os
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


def _default_settings_file() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _hook_command() -> str:
    """The shell command Claude Code should run per event. Resolved to an
    absolute path at install time: hooks run with whatever PATH the host
    session has, which need not include this install's Scripts dir."""
    exe = shutil.which(_MARKER) or _MARKER
    quoted = f'"{exe}"' if " " in exe else exe
    return f"{quoted} --source claude"


def _codex_recipe() -> str:
    exe = shutil.which(_MARKER) or _MARKER
    return f'notify = [{json.dumps(exe)}, "--source", "codex"]'


def _load_settings(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("settings.json is not a JSON object")
    return data


def _event_wired(entries: object) -> bool:
    return isinstance(entries, list) and any(_MARKER in json.dumps(e) for e in entries)


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
    try:
        data = _load_settings(path)
    except (ValueError, TypeError) as exc:
        click.echo(f"  {style('x', fg='red')} Cannot edit {path}: {exc}", err=True)
        raise SystemExit(1) from exc

    hooks_raw = data.get("hooks")
    hooks: dict[str, object] = hooks_raw if isinstance(hooks_raw, dict) else {}  # ty: ignore[invalid-assignment]  # reason: known ty 0.0.59 isinstance-dict narrowing gap
    data["hooks"] = hooks
    cmd = _hook_command()
    added: list[str] = []
    for event in _EVENTS:
        entries_raw = hooks.get(event)
        entries: list[object] = entries_raw if isinstance(entries_raw, list) else []  # ty: ignore[invalid-assignment]  # reason: known ty 0.0.59 isinstance-list narrowing gap
        hooks[event] = entries
        if _event_wired(entries):
            continue
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": cmd, "timeout": 10}]
        }
        if event == "PostToolUse":
            entry = {"matcher": "*", **entry}
        entries.append(entry)
        added.append(event)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    if added:
        click.echo(
            f"  {style('+', fg='green', bold=True)} Wired {', '.join(added)} in {style(str(path), dim=True)}"
        )
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
    try:
        data = _load_settings(path)
    except (ValueError, TypeError):
        data = {}
    hooks = data.get("hooks")
    hooks_map = hooks if isinstance(hooks, dict) else {}
    for event in _EVENTS:
        wired = _event_wired(hooks_map.get(event))
        mark = style("+", fg="green", bold=True) if wired else style("x", fg="red")
        click.echo(f"  {mark} {event}")
    click.echo()
    records = agent_state.all_states()
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
