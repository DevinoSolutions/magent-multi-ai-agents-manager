"""The interactive config editor (`_config_menu`, radon F/48 -- the single
worst-graded function in the codebase, relocated unchanged per E6.md S2.5:
"do not smuggle a rewrite into a move") and the `magent config` command
group with all 14 subcommands (13 original + E7's `migrate`).
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import click

from magent.cli.app import main
from magent.cli.config_io import (
    _as_dict,
    _as_int,
    _as_str,
    _load_raw_config,
    _project_dicts,
    _save_raw_config,
    _sub,
    _sublist,
)
from magent.cli.ui import (
    _banner,
    _confirm_change,
    _divider,
    _grid_preview,
    _menu_item,
    _open_in_editor,
    _prompt_or_back,
)
from magent.config import _derive_tab_color, migrate_config_file
from magent.paths import find_config
from magent.style import style


def _config_menu(config_file: Path) -> None:
    """Interactive config editor accessed from the main menu."""
    while True:
        click.clear()
        _banner()
        data = _load_raw_config(config_file)
        layout = _as_dict(data.get("layout"))
        settings = _as_dict(data.get("settings"))
        projects = _project_dicts(data)
        tools = _as_dict(settings.get("tools"))
        cols, rows = _as_int(layout.get("columns"), 2), _as_int(layout.get("rows"), 1)
        dtool = _as_str(settings.get("defaultTool"), "claude")
        total_slots = cols * rows

        click.echo()
        click.echo(f"  {style('Settings', bold=True)}")
        _divider()
        click.echo()
        click.echo(f"  {style('Each screen is tiled like this:', dim=True)}")
        click.echo()
        for line in _grid_preview(cols, rows, indent="      "):
            click.echo(line)
        click.echo()
        _menu_item(
            "1",
            f"Window grid      {style(f'{cols} cols x {rows} rows', fg='green')}"
            f"  {style(f'= {total_slots} windows per screen', dim=True)}",
        )
        _menu_item(
            "2",
            f"Default AI tool   {style(dtool, fg='green')}"
            f"  {style('-- launched in each project', dim=True)}",
        )
        base = _as_str(data.get("baseDir"))
        if base:
            short = base if len(base) <= 35 else "..." + base[-32:]
            _menu_item("3", f"Projects folder   {style(short, fg='green')}")
        else:
            _menu_item(
                "3",
                f"Projects folder   {style('(not set -- using absolute paths)', dim=True)}",
            )
        _menu_item(
            "4",
            f"Tool commands     {style(', '.join(tools.keys()) or '(none)', dim=True)}"
            f"  {style('-- what runs in the terminal', dim=True)}",
        )
        happy_on = settings.get("happy", False)
        happy_label = (
            style("ON", fg="green", bold=True) if happy_on else style("off", dim=True)
        )
        _menu_item(
            "5",
            f"Happy mobile      {happy_label}"
            f"  {style('-- monitor sessions from phone/web', dim=True)}",
        )
        psmux_on = settings.get("psmux", False)
        psmux_label = (
            style("ON", fg="green", bold=True) if psmux_on else style("off", dim=True)
        )
        _menu_item(
            "6",
            f"psmux sessions    {psmux_label}"
            f"  {style('-- attach from SSH / phone', dim=True)}",
        )
        upload_on = settings.get("uploadServer", False)
        upload_port = settings.get("uploadPort", 8033)
        upload_label = (
            style(f"ON :{upload_port}", fg="green", bold=True)
            if upload_on
            else style("off", dim=True)
        )
        _menu_item(
            "7",
            f"Upload server     {upload_label}"
            f"  {style('-- send images from phone to Claude', dim=True)}",
        )
        click.echo()
        click.echo(f"  {style('Projects', bold=True)}")
        _divider()
        click.echo()
        _menu_item(
            "8", f"Add a project     {style('-- register a new folder', dim=True)}"
        )
        _menu_item(
            "9", f"Remove a project  {style(f'({len(projects)} configured)', dim=True)}"
        )
        click.echo()
        _menu_item("0", "Open config file in editor", key_fg="green")
        _menu_item("b", "Back to main menu", key_fg="yellow")
        click.echo()

        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="b",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )

        if choice == "1":
            click.echo()
            click.echo(f"  {style('How many windows per screen?', bold=True)}")
            click.echo(
                f"  {style('Columns = side by side, Rows = stacked.', dim=True)}"
            )
            click.echo()
            val = _prompt_or_back("Columns (side by side)", default=str(cols))
            if val is None:
                continue
            try:
                new_cols = max(1, int(val))
            except ValueError:
                continue
            val = _prompt_or_back("Rows (stacked)", default=str(rows))
            if val is None:
                continue
            try:
                new_rows = max(1, int(val))
            except ValueError:
                continue
            click.echo()
            click.echo(f"  {style('Your screens will look like:', bold=True)}")
            click.echo()
            for line in _grid_preview(new_cols, new_rows, indent="      "):
                click.echo(line)
            layout_d = _sub(data, "layout")
            layout_d["columns"] = new_cols
            layout_d["rows"] = new_rows
            _save_raw_config(config_file, data)
            _confirm_change(
                f"Window grid set to {style(f'{new_cols} x {new_rows}', fg='green')}"
                f" ({new_cols * new_rows} windows per screen)."
            )

        elif choice == "2":
            click.echo()
            click.echo(
                f"  {style('Which AI tool should open in each project by default?', bold=True)}"
            )
            click.echo(f"  {style('Individual projects can override this.', dim=True)}")
            click.echo()
            available = list(tools.keys()) or ["claude", "codex"]
            for i, t in enumerate(available, 1):
                marker = style(" <-- current", dim=True) if t == dtool else ""
                _menu_item(str(i), f"{t}{marker}")
            click.echo()
            val = _prompt_or_back(
                "Pick a number or type a name", default=dtool, show_default=False
            )
            if val is None:
                continue
            try:
                idx = int(val) - 1
                if 0 <= idx < len(available):
                    val = available[idx]
            except ValueError:
                pass
            _sub(data, "settings")["defaultTool"] = val
            _save_raw_config(config_file, data)
            _confirm_change(f"Default tool set to {style(val, fg='green')}.")

        elif choice == "3":
            click.echo()
            click.echo(f"  {style('Where are your projects?', bold=True)}")
            click.echo(
                f"  {style('Project paths in your config are relative to this folder.', dim=True)}"
            )
            click.echo(
                f"  {style('Example: if base is C:/projects and a project path is', dim=True)}"
            )
            click.echo(
                f"  {style('api/backend, it opens C:/projects/api/backend.', dim=True)}"
            )
            click.echo()
            val = _prompt_or_back(
                "Projects folder", default=_as_str(data.get("baseDir"))
            )
            if val is None or not val:
                continue
            normalized = val.replace("\\", "/")
            data["baseDir"] = normalized
            _save_raw_config(config_file, data)
            _confirm_change(f"Projects folder set to {style(normalized, fg='green')}.")

        elif choice == "4":
            _tools_menu(config_file, data)

        elif choice == "5":
            settings_d = _sub(data, "settings")
            new_val = not settings_d.get("happy", False)
            settings_d["happy"] = new_val
            _save_raw_config(config_file, data)
            if new_val:
                _confirm_change(
                    f"Happy mobile {style('enabled', fg='green')}. "
                    f"Sessions will be accessible from your phone via the Happy app."
                )
            else:
                _confirm_change(
                    f"Happy mobile {style('disabled', dim=True)}. "
                    f"Sessions launch directly without Happy."
                )

        elif choice == "6":
            settings_d = _sub(data, "settings")
            new_val = not settings_d.get("psmux", False)
            settings_d["psmux"] = new_val
            _save_raw_config(config_file, data)
            if new_val:
                _confirm_change(
                    f"psmux sessions {style('enabled', fg='green')}. "
                    f"Each project runs in a named psmux session you can attach to via SSH."
                )
            else:
                _confirm_change(
                    f"psmux sessions {style('disabled', dim=True)}. "
                    f"Projects launch in regular Windows Terminal tabs."
                )

        elif choice == "7":
            settings_d = _sub(data, "settings")
            currently_on = settings_d.get("uploadServer", False)
            if currently_on:
                settings_d["uploadServer"] = False
                _save_raw_config(config_file, data)
                _confirm_change(f"Upload server {style('disabled', dim=True)}.")
            else:
                click.echo()
                cur_port = settings_d.get("uploadPort", 8033)
                val = _prompt_or_back(
                    f"Port {style(f'(Enter for {cur_port})', dim=True)}",
                    default=str(cur_port),
                    show_default=False,
                )
                if val is None:
                    continue
                try:
                    port = int(val)
                except ValueError:
                    continue
                settings_d["uploadServer"] = True
                settings_d["uploadPort"] = port
                _save_raw_config(config_file, data)
                _confirm_change(
                    f"Upload server {style('enabled', fg='green')} on port {style(str(port), fg='cyan')}. "
                    f"Starts automatically with magent."
                )

        elif choice == "8":
            cwd = str(Path.cwd()).replace("\\", "/")
            click.echo()
            click.echo(
                f"  {style('Add a project folder for magent to open.', bold=True)}"
            )
            click.echo(
                f"  {style('Path can be absolute or relative to your projects folder.', dim=True)}"
            )
            click.echo(f"  {style('Press Enter to use the current folder.', dim=True)}")
            click.echo()
            path = _prompt_or_back("Folder path", default=cwd)
            if path is None or not path:
                continue
            entry: dict[str, object] = {"path": path.replace("\\", "/")}
            click.echo()
            click.echo(
                f"  {style('Optional settings (Enter to skip, b to cancel):', dim=True)}"
            )
            click.echo()
            group = _prompt_or_back(
                f"Group {style('-- for launching subsets, e.g. INTERNAL', dim=True)}",
                default="",
                show_default=False,
            )
            if group is None:
                continue
            if group:
                entry["group"] = group
            tool = _prompt_or_back(
                f"Tool  {style('-- override default, e.g. codex, vscode', dim=True)}",
                default="",
                show_default=False,
            )
            if tool is None:
                continue
            if tool:
                entry["tool"] = tool
            color = _prompt_or_back(
                f"Color {style('-- terminal tab color, Enter for random', dim=True)}",
                default="",
                show_default=False,
            )
            if color is None:
                continue
            if not color:
                used = {
                    c for p in _project_dicts(data) if (c := _as_str(p.get("color")))
                }
                color = _derive_tab_color(str(entry.get("title") or path), used)
            entry["color"] = color
            _sublist(data, "projects").append(entry)
            _save_raw_config(config_file, data)
            _confirm_change(f"Added project {style(path, fg='green')}.")

        elif choice == "9":
            _remove_project_menu(config_file, data)

        elif choice == "0":
            _open_in_editor(config_file)
            return

        elif choice == "b":
            return


def _tools_menu(config_file: Path, data: dict[str, object]) -> None:
    tools = _as_dict(_as_dict(data.get("settings")).get("tools"))
    while True:
        click.clear()
        _banner()
        click.echo(f"  {style('Tool Commands', bold=True)}")
        click.echo(
            f"  {style('Each tool name maps to the shell command that runs inside', dim=True)}"
        )
        tools_hint = 'the terminal. e.g. "claude" runs "claude --continue".'
        click.echo(f"  {style(tools_hint, dim=True)}")
        _divider()
        click.echo()
        for name, cmd in tools.items():
            click.echo(f"    {style(name, fg='cyan'):<20} -> {style(cmd, dim=True)}")
        if not tools:
            click.echo(f"    {style('(no tools configured)', dim=True)}")
        click.echo()
        _menu_item("a", "Add or edit a tool")
        _menu_item("r", "Remove a tool")
        _menu_item("b", "Back", key_fg="yellow")
        click.echo()

        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="b",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )

        if choice == "a":
            click.echo()
            click.echo(
                f"  {style('Name is a short label (e.g. aider, shell).', dim=True)}"
            )
            click.echo(
                f"  {style('Command is what runs in the terminal (e.g. aider --model sonnet).', dim=True)}"
            )
            click.echo()
            tool_name = _prompt_or_back("Tool name")
            if not tool_name:
                continue
            existing = _as_str(tools.get(tool_name))
            cmd = _prompt_or_back("Shell command to run", default=existing)
            if cmd is None:
                continue
            tools_d = _sub(_sub(data, "settings"), "tools")
            tools_d[tool_name] = cmd
            tools = tools_d
            _save_raw_config(config_file, data)
            _confirm_change(
                f"Tool {style(name, fg='green')} set to {style(cmd, dim=True)}."
            )

        elif choice == "r":
            if not tools:
                click.echo(f"  {style('No tools to remove.', dim=True)}")
                continue
            click.echo()
            tool_names = list(tools.keys())
            for i, name in enumerate(tool_names, 1):
                _menu_item(str(i), name)
            click.echo()
            val = _prompt_or_back("Remove which?")
            if val is None:
                continue
            try:
                idx = int(val) - 1
                if 0 <= idx < len(tool_names):
                    removed_name = tool_names[idx]
                    del tools[removed_name]
                    _save_raw_config(config_file, data)
                    _confirm_change(f"Removed tool {style(removed_name, fg='green')}.")
            except (ValueError, IndexError):
                click.echo(f"  {style('x', fg='red')} Invalid choice.")

        elif choice == "b":
            return


def _remove_project_menu(config_file: Path, data: dict[str, object]) -> None:
    projects = _sublist(data, "projects")
    if not projects:
        click.echo(f"  {style('No projects to remove.', dim=True)}")
        return

    click.clear()
    _banner()
    for i, raw in enumerate(projects, 1):
        p = _as_dict(raw)
        leaf = Path(_as_str(p.get("path"), "?")).name
        extra = ""
        if p.get("group"):
            extra = f"  {style(_as_str(p.get('group')), dim=True)}"
        if not p.get("enabled", True):
            extra += f"  {style('disabled', fg='red')}"
        click.echo(f"   {style(str(i).rjust(2), dim=True)}  {leaf:<30}{extra}")

    click.echo()
    val = _prompt_or_back("Remove which?")
    if val is None:
        return
    try:
        idx = int(val) - 1
        if 0 <= idx < len(projects):
            removed = _as_dict(projects.pop(idx))
            _save_raw_config(config_file, data)
            _confirm_change(
                f"Removed project "
                f"{style(Path(_as_str(removed.get('path'), '?')).name, fg='green')}."
            )
        else:
            click.echo(f"  {style('x', fg='red')} Invalid number.")
    except ValueError:
        click.echo(f"  {style('x', fg='red')} Invalid choice.")


@main.group()
@click.pass_context
def config(ctx: click.Context) -> None:
    """View and modify your magent configuration."""


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Display current configuration."""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)

    _banner()
    click.echo(f"  {style('Config:', bold=True)} {style(str(config_file), dim=True)}")
    click.echo()

    click.echo(
        f"  {style('Base dir:', bold=True)}     {data.get('baseDir', style('(not set)', dim=True))}"
    )

    layout = _as_dict(data.get("layout"))
    cols, rows = _as_int(layout.get("columns"), 2), _as_int(layout.get("rows"), 1)
    click.echo(f"  {style('Layout:', bold=True)}       {cols} x {rows}")

    settings = _as_dict(data.get("settings"))
    click.echo(
        f"  {style('Default tool:', bold=True)} {settings.get('defaultTool', 'claude')}"
    )
    click.echo()

    tools = _as_dict(settings.get("tools"))
    if tools:
        click.echo(f"  {style('Tools:', bold=True)}")
        for name, cmd in tools.items():
            click.echo(f"    {style(name, fg='cyan'):<20} {style(cmd, dim=True)}")
        click.echo()

    projects = _project_dicts(data)
    click.echo(f"  {style('Projects:', bold=True)} {len(projects)}")
    for p in projects:
        tool = p.get("tool", "")
        group = p.get("group", "")
        enabled = p.get("enabled", True)

        leaf = Path(_as_str(p.get("path"), "?")).name
        parts = []
        if tool:
            parts.append(style(tool, fg="cyan"))
        if group:
            parts.append(style(group, dim=True))
        if not enabled:
            parts.append(style("disabled", fg="red"))
        extra = f"  {' | '.join(parts)}" if parts else ""
        click.echo(f"    {leaf:<30}{extra}")

    click.echo()


@config.command("migrate")
@click.pass_context
def config_migrate(ctx: click.Context) -> None:
    """Migrate the config file to the current schema version, persisting any backfilled colors."""
    config_file = find_config(ctx.obj.get("config_path"))
    try:
        changed = migrate_config_file(str(config_file))
    except (ValueError, FileNotFoundError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if changed:
        click.echo(f"  Migrated {style(str(config_file), dim=True)}")
    else:
        click.echo("  Already up to date; nothing to migrate.")


@config.command("layout")
@click.argument("columns", type=int)
@click.argument("rows", type=int)
@click.pass_context
def config_layout(ctx: click.Context, columns: int, rows: int) -> None:
    """Set grid layout. Usage: magent config layout 3 2"""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    layout_d = _sub(data, "layout")
    layout_d["columns"] = max(1, columns)
    layout_d["rows"] = max(1, rows)
    _save_raw_config(config_file, data)
    click.echo(f"  Layout set to {columns} x {rows}")


@config.command("base-dir")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def config_base_dir(ctx: click.Context, path: str) -> None:
    """Set the base directory for project paths."""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    resolved = str(Path(path).resolve()).replace("\\", "/")
    data["baseDir"] = resolved
    _save_raw_config(config_file, data)
    click.echo(f"  Base dir set to {resolved}")


@config.command("default-tool")
@click.argument("tool")
@click.pass_context
def config_default_tool(ctx: click.Context, tool: str) -> None:
    """Set the default tool for new projects."""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    _sub(data, "settings")["defaultTool"] = tool
    _save_raw_config(config_file, data)
    click.echo(f"  Default tool set to {style(tool, fg='cyan')}")


@config.command("tool")
@click.argument("name")
@click.argument("command")
@click.pass_context
def config_tool(ctx: click.Context, name: str, command: str) -> None:
    """Add or update a tool command. Usage: magent config tool aider 'aider --model sonnet'"""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    _sub(_sub(data, "settings"), "tools")[name] = command
    _save_raw_config(config_file, data)
    click.echo(f"  Tool {style(name, fg='cyan')} = {style(command, dim=True)}")


@config.command("remove-tool")
@click.argument("name")
@click.pass_context
def config_remove_tool(ctx: click.Context, name: str) -> None:
    """Remove a tool."""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    tools = _as_dict(_as_dict(data.get("settings")).get("tools"))
    if name not in tools:
        click.echo(f"  Tool '{name}' not found.", err=True)
        sys.exit(1)
    del tools[name]
    _save_raw_config(config_file, data)
    click.echo(f"  Removed tool {style(name, fg='cyan')}")


@config.command("add")
@click.argument("path")
@click.option("--group", "-g", default=None, help="Group name")
@click.option("--tool", "-t", default=None, help="Tool (claude, codex, vscode, ...)")
@click.option("--color", "-c", default=None, help="Tab color (#rrggbb)")
@click.option("--title", default=None, help="Custom window title")
@click.option("--host", default=None, help="SSH host for remote projects")
@click.option("--windows", "-w", default=None, type=int, help="Number of windows")
@click.pass_context
def config_add(
    ctx: click.Context,
    path: str,
    group: str | None,
    tool: str | None,
    color: str | None,
    title: str | None,
    host: str | None,
    windows: int | None,
) -> None:
    """Add a project. Usage: magent config add ./myapp -g INTERNAL -t claude"""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    entry: dict[str, object] = {"path": path.replace("\\", "/")}
    if group:
        entry["group"] = group
    if tool:
        entry["tool"] = tool
    if color:
        entry["color"] = color
    if title:
        entry["title"] = title
    if host:
        entry["host"] = host
    if windows:
        entry["windows"] = windows

    _sublist(data, "projects").append(entry)
    _save_raw_config(config_file, data)
    click.echo(f"  Added {style(path, fg='cyan')}")


@config.command("remove")
@click.argument("path")
@click.pass_context
def config_remove(ctx: click.Context, path: str) -> None:
    """Remove a project by path (or leaf name)."""
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    projects = _project_dicts(data)
    normalized = path.replace("\\", "/")

    before = len(projects)
    kept = [
        p
        for p in projects
        if _as_str(p.get("path")) != normalized
        and Path(_as_str(p.get("path"))).name != path
    ]
    data["projects"] = kept

    removed = before - len(kept)
    if removed == 0:
        click.echo(f"  No project matching '{path}' found.", err=True)
        sys.exit(1)

    _save_raw_config(config_file, data)
    click.echo(f"  Removed {removed} project(s) matching {style(path, fg='cyan')}")


@config.command("enable")
@click.argument("path")
@click.pass_context
def config_enable(ctx: click.Context, path: str) -> None:
    """Enable a disabled project."""
    _set_project_field(ctx, path, "enabled", True)
    click.echo(f"  Enabled {style(path, fg='cyan')}")


@config.command("disable")
@click.argument("path")
@click.pass_context
def config_disable(ctx: click.Context, path: str) -> None:
    """Disable a project without removing it."""
    _set_project_field(ctx, path, "enabled", False)
    click.echo(f"  Disabled {style(path, fg='cyan')}")


@config.command("set")
@click.argument("path")
@click.argument("field")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, path: str, field: str, value: str) -> None:
    """Set a field on a project. Usage: magent config set myapp group INTERNAL"""
    parsed: str | int | bool = value
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        with contextlib.suppress(ValueError):
            parsed = int(value)
    _set_project_field(ctx, path, field, parsed)
    click.echo(
        f"  Set {style(field, bold=True)} = {style(str(value), fg='cyan')} on {path}"
    )


@config.command("open")
@click.pass_context
def config_open(ctx: click.Context) -> None:
    """Open config file in your default editor."""
    config_file = find_config(ctx.obj.get("config_path"))
    if not config_file.exists():
        click.echo(f"No config at {config_file}. Run magent first.", err=True)
        sys.exit(1)
    _open_in_editor(config_file)
    click.echo(f"  Opened {style(str(config_file), dim=True)}")


@config.command("path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Print the config file path."""
    click.echo(str(find_config(ctx.obj.get("config_path"))))


def _set_project_field(
    ctx: click.Context, path: str, field: str, value: object
) -> None:
    config_file = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(config_file)
    normalized = path.replace("\\", "/")

    found = False
    for p in _project_dicts(data):
        if (
            _as_str(p.get("path")) == normalized
            or Path(_as_str(p.get("path"))).name == path
        ):
            p[field] = value
            found = True
            break

    if not found:
        click.echo(f"  No project matching '{path}' found.", err=True)
        sys.exit(1)

    _save_raw_config(config_file, data)
