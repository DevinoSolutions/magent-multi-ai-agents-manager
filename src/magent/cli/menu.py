"""The interactive main menu and the first-run discovery wizard. No
`@main.command` decorators here (neither is a click command) -- app.py's
callback reaches both via an in-body import (cycle-break, E6.md S2.1), and
_show_menu reaches the config editor the same way: this module imports
config_editor at its own top level (menu -> config_editor -> app, no back
edge -- config_editor never imports menu).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from magent.cli.config_editor import _config_menu
from magent.cli.ui import _banner, _divider, _menu_item, _open_in_editor
from magent.paths import _config_path
from magent.style import style


def _show_menu(groups: list[str], config_file: Path | None = None) -> dict[str, object]:
    config_changed = False
    while True:
        click.clear()
        _banner()
        _divider()
        click.echo()
        _menu_item(
            "1", "Launch & tile new windows", extra=style("  (default)", dim=True)
        )
        _menu_item("2", "Re-tile all open windows")
        if groups:
            group_list = style(f"  {' | '.join(groups)}", dim=True)
            _menu_item("3", "Launch a group" + group_list)
        click.echo()
        _menu_item(
            "u",
            "Bring up sessions in background",
            key_fg="cyan",
            extra=style("  (no windows)", dim=True),
        )
        _menu_item(
            "s",
            "Open session switcher",
            key_fg="cyan",
            extra=style("  (one window, switch inside)", dim=True),
        )
        _menu_item(
            "a",
            "Attach to a remote host",
            key_fg="cyan",
            extra=style("  (SSH to another PC)", dim=True),
        )
        click.echo()
        _menu_item("t", "Status", extra=style("  (what's running)", dim=True))
        _menu_item("d", "Shut down sessions", key_fg="yellow")
        click.echo()
        _menu_item("e", "Edit config", key_fg="yellow")
        _menu_item("q", "Quit", key_fg="red")
        click.echo()

        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="1",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )

        if choice == "1":
            return {
                "action": "run",
                "retile_all": False,
                "group": None,
                "reload": config_changed,
            }
        if choice == "2":
            return {
                "action": "run",
                "retile_all": True,
                "group": None,
                "reload": config_changed,
            }
        if choice == "u":
            return {"action": "up", "reload": config_changed}
        if choice == "s":
            return {"action": "sessions", "reload": config_changed}
        if choice == "a":
            return {"action": "attach", "reload": config_changed}
        if choice == "t":
            return {"action": "status", "reload": config_changed}
        if choice == "d":
            return {"action": "down", "reload": config_changed}
        if choice == "3" and groups:
            click.echo()
            for i, g in enumerate(groups, 1):
                _menu_item(str(i), g)
            click.echo()
            idx_str = click.prompt(
                f"  {style('group', fg='cyan')}",
                default="1",
                show_default=False,
                prompt_suffix=" ",
            ).strip()
            try:
                idx = int(idx_str) - 1
                if 0 <= idx < len(groups):
                    return {
                        "action": "run",
                        "retile_all": False,
                        "group": groups[idx],
                        "reload": config_changed,
                    }
            except ValueError:
                pass
            click.echo(f"\n  {style('x', fg='red')} Invalid choice.\n")
        elif choice == "e":
            if config_file and config_file.exists():
                _config_menu(config_file)
                data = json.loads(config_file.read_text(encoding="utf-8"))
                projects = data.get("projects", [])
                groups = sorted(
                    {p.get("group", "") for p in projects if p.get("group")}
                )
                config_changed = True
            else:
                _open_in_editor(config_file or _config_path())
        elif choice == "q":
            return {"action": "quit", "reload": False}
        else:
            click.echo(f"\n  {style('x', fg='red')} Invalid choice.\n")


def _run_discovery(config_file: Path) -> bool:
    from magent.discover import discover_projects, projects_to_config

    _banner()
    click.echo(
        f"  {style('Welcome!', fg='green', bold=True)} magent opens a terminal for each of your"
    )
    click.echo("  projects, launches your AI agent inside it, and tiles")
    click.echo("  all windows neatly across your screens.")
    click.echo()
    click.echo("  Scanning your recent sessions to find your projects...")
    click.echo()

    projects, days = discover_projects()

    if not projects:
        click.echo(
            f"  {style('No projects found', dim=True)} in Claude, Codex, or VS Code history."
        )
        click.echo(
            f"  Create a config manually at: {style(str(config_file), bold=True)}"
        )
        return False

    click.echo(
        f"  Found {style(str(len(projects)), fg='green', bold=True)} projects from the last {days} days:\n"
    )

    tool_colors = {"claude": "magenta", "codex": "cyan", "vscode": "blue"}
    for i, p in enumerate(projects):
        path = p["path"]
        tool = p["tool"]
        leaf = Path(path).name if isinstance(path, str) else ""
        tc = tool_colors.get(tool, "white") if isinstance(tool, str) else "white"
        badge = style(f"[{tool}]", fg=tc, dim=True)
        num = style(f"{i + 1:>2}", dim=True)
        click.echo(f"   {num}  {leaf:<34} {badge}")

    click.echo()
    _divider()
    click.echo()

    if not click.confirm(
        f"  Generate config with these {len(projects)} projects?", default=True
    ):
        click.echo(f"\n  {style('Cancelled.', dim=True)}")
        return False

    config = projects_to_config(projects)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

    click.echo(
        f"\n  {style('+', fg='green', bold=True)} Saved to {style(str(config_file), fg='cyan')}"
    )
    click.echo()
    click.echo(f"  Run {style('magent', bold=True)} again to launch all your projects")
    click.echo("  and tile them across your screens.")
    click.echo()
    click.echo(f"  To tweak the config: {style('magent config show', fg='cyan')}")
    click.echo()

    return True
