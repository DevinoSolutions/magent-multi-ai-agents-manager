"""The `magent docs` command: a 190-line pure-string Markdown generator
for the full config reference. No I/O beyond stdout via click.echo.
"""

from __future__ import annotations

import click

from magent import __version__
from magent.cli.app import main
from magent.config import LayoutConfig, Settings

_PROJECT_FIELD_DOCS: list[tuple[str, str, str, str]] = [
    ("path", "string", "*(required)*", "Absolute, or relative to `baseDir`."),
    ("group", "string", "none", "Tag for group launches (`-g`)."),
    (
        "tool",
        "string",
        "`defaultTool`",
        "`claude`, `codex`, `cursor-agent`, `agy`, `vscode`, `cursor`, or any custom tool.",
    ),
    ("color", "string", "derived", "Terminal tab color (`#rrggbb`)."),
    ("title", "string", "folder name", "Window title for matching."),
    ("enabled", "boolean", "`true`", "Set `false` to skip without deleting."),
    ("happy", "boolean", "inherit", "Override global Happy setting for this project."),
    ("host", "string", "none", "SSH target for remote projects."),
    ("remotePath", "string", "`path`", "Remote directory when different from `path`."),
    (
        "windows",
        "list",
        "none",
        'List of window objects `{"name", "tool", "command"}` with per-window tool/command overrides. Legacy `int` / `["name1", "name2"]` forms still parse (normalized by `magent config migrate`).',
    ),
]


_SETTINGS_FIELD_DOCS: list[tuple[str, str, str, str]] = [
    (
        "defaultTool",
        "string",
        '`"claude"`',
        "AI tool launched in each project unless overridden.",
    ),
    (
        "settleSeconds",
        "int",
        "`3`",
        "Seconds to wait for windows to appear before tiling.",
    ),
    ("launchDelayMs", "int", "`400`", "Delay between launching each terminal (ms)."),
    (
        "happy",
        "boolean",
        "`false`",
        "Enable [Happy](https://github.com/slopus/happy) to access sessions from mobile/web.",
    ),
    (
        "psmux",
        "boolean",
        "`false`",
        "Run CLI agents in psmux sessions (Windows). Attach from SSH with `psmux attach -t <name>`.",
    ),
    (
        "uploadServer",
        "boolean",
        "`false`",
        "Auto-start upload server for mobile image transfer when psmux launches.",
    ),
    ("uploadPort", "int", "`8033`", "Port for the upload server."),
    (
        "windowTitlePrefix",
        "boolean",
        "`true`",
        "Prefix every window title with `magent:` so the attention daemon's "
        "badges, the Alt+V hotkey, and `magent-name` tiling can recognize magent "
        "windows. Set `false` for bare project-name titles — then title badges, "
        "the Alt+V hotkey, and `project_from_title` no-op, and launch-path tiling "
        "falls back to exact-title matching. `magent attach` windows always keep "
        "the prefix: there the title carries the psmux session id (P3-01), so it "
        "is load-bearing, not cosmetic.",
    ),
    (
        "tools",
        "object",
        '`{"claude": ..., "codex": ..., "cursor-agent": ..., "agy": ...}`',
        "Map of tool names to shell commands. Add custom tools here.",
    ),
    ("ssh.shell", "string", '`"bash -lc"`', "Shell wrapper for remote SSH commands."),
    (
        "attention.badge",
        "boolean",
        "`true`",
        "Attention daemon rewrites window titles with a state badge (`magent:[!] name`).",
    ),
    (
        "attention.flash",
        "boolean",
        "`true`",
        "Flash the taskbar button when a session needs input or errors.",
    ),
    (
        "attention.toast",
        "boolean",
        "`false`",
        "Windows toast on needs-input/error (requires the optional `winotify` extra).",
    ),
    (
        "attention.ntfy",
        "boolean",
        "`false`",
        "Push needs-input/error to an ntfy topic (set `MAGENT_NTFY_TOPIC`).",
    ),
    (
        "attention.notifyOnDone",
        "boolean",
        "`false`",
        "Also push toast/ntfy when an agent finishes (enters `done`). Opt-in; "
        "does nothing unless `toast` or `ntfy` is on.",
    ),
]


def _generate_docs() -> str:
    defaults_layout = LayoutConfig()
    defaults_settings = Settings()

    config_locations = {
        "Windows": r"`%APPDATA%\magent\config.json`",
        "macOS": "`~/Library/Application Support/magent/config.json`",
        "Linux": "`~/.config/magent/config.json`",
    }

    lines: list[str] = []
    w = lines.append

    w("# magent Configuration Reference")
    w("")
    w(f"*Generated from magent v{__version__} schema.*")
    w("")

    w("## Config file location")
    w("")
    for platform, loc in config_locations.items():
        w(f"- **{platform}:** {loc}")
    w("")
    w("Or place `magent.config.json` in your working directory (takes priority).")
    w("")

    w("## Top-level fields")
    w("")
    w("| Field | Type | Default | Description |")
    w("| --- | --- | --- | --- |")
    w(
        "| `baseDir` | string | none | Root folder. Project paths are relative to this. |"
    )
    w(
        f"| `layout.columns` | int | `{defaults_layout.columns}` | Windows side by side per screen. |"
    )
    w(
        f"| `layout.rows` | int | `{defaults_layout.rows}` | Windows stacked per screen. |"
    )
    w("| `projects` | array | *(required)* | List of project entries (see below). |")
    w("| `settings` | object | see below | Global settings. |")
    w("")

    w("## Settings")
    w("")
    w('All fields under `"settings"` in config.json:')
    w("")
    w("| Field | Type | Default | Description |")
    w("| --- | --- | --- | --- |")
    for name, type_, default, desc in _SETTINGS_FIELD_DOCS:
        w(f"| `{name}` | {type_} | {default} | {desc} |")
    w("")

    w("## Project fields")
    w("")
    w('Each entry in the `"projects"` array:')
    w("")
    w("| Field | Type | Default | Description |")
    w("| --- | --- | --- | --- |")
    for name, type_, default, desc in _PROJECT_FIELD_DOCS:
        w(f"| `{name}` | {type_} | {default} | {desc} |")
    w("")

    w("## Example config")
    w("")
    w("```json")
    w("{")
    w('  "baseDir": "C:/Users/you/projects",')
    w('  "layout": { "columns": 2, "rows": 1 },')
    w('  "settings": {')
    w(f'    "defaultTool": "{defaults_settings.default_tool}",')
    w(f'    "settleSeconds": {defaults_settings.settle_seconds},')
    w(f'    "launchDelayMs": {defaults_settings.launch_delay_ms},')
    # Derive the tools block straight from the factory defaults so the example
    # can never drift from DEFAULT_TOOLS (NF-S3-003 -- no fabricated tools).
    w('    "tools": {')
    tool_items = list(defaults_settings.tools.items())
    for i, (name, cmd) in enumerate(tool_items):
        trailing = "," if i < len(tool_items) - 1 else ""
        w(f'      "{name}": "{cmd}"{trailing}')
    w("    }")
    w("  },")
    w('  "projects": [')
    w('    { "path": "api", "group": "INTERNAL", "color": "#3b82f6" },')
    w('    { "path": "web", "group": "INTERNAL", "tool": "codex" },')
    w('    { "path": "docs", "tool": "vscode" }')
    w("  ]")
    w("}")
    w("```")
    w("")

    w("## Multi-window sessions")
    w("")
    w(
        "Open the same project in multiple windows. `windows` is a list of window "
        "objects, each with optional per-window `tool`/`command` overrides:"
    )
    w("")
    w("```json")
    w("{")
    w('  "path": "api",')
    w('  "windows": [')
    w('    { "name": "api" },')
    w('    { "name": "api-2" },')
    w('    { "name": "api-codex", "tool": "codex" }')
    w("  ]")
    w("}")
    w("```")
    w("")
    w(
        "`name` sets the window title; `tool`/`command` override the project's "
        "defaults for that window only. Windows without an override each resume "
        "the Nth most recent session for the project's tool."
    )
    w("")
    w(
        'The legacy `"windows": 3` and `"windows": ["api", "api-2"]` forms still '
        "parse and are normalized to window objects by `magent config migrate`."
    )
    w("")

    w("## Remote projects (SSH)")
    w("")
    w("```json")
    w('{ "host": "deploy@server", "path": "/srv/api", "tool": "claude" }')
    w("```")
    w("")
    w("CLI agents run over SSH. VS Code projects open via Remote-SSH.")
    w("")

    w("## Happy (mobile/web access)")
    w("")
    w(
        "Enable [Happy](https://github.com/slopus/happy) to monitor and control your AI sessions"
    )
    w(
        "from your phone or any browser. Happy wraps supported agents (claude, codex) and relays"
    )
    w("encrypted session data to the Happy mobile/web app.")
    w("")
    w("```json")
    w('"settings": {')
    w('  "happy": true')
    w("}")
    w("```")
    w("")
    w("Requires `npm install -g happy`. Per-project override:")
    w("")
    w("```json")
    w('{ "path": "api", "happy": true }')
    w('{ "path": "docs", "tool": "vscode", "happy": false }')
    w("```")
    w("")

    w("## Custom tools")
    w("")
    w("Add any command under `settings.tools`:")
    w("")
    w("```json")
    w('"tools": {')
    w('  "claude": "claude --continue",')
    w('  "codex": "codex",')
    w('  "cursor-agent": "cursor-agent",')
    w('  "agy": "agy",')
    w('  "aider": "aider --model sonnet",')
    w('  "shell": "bash"')
    w("}")
    w("```")
    w("")
    w('Then use `"tool": "aider"` on any project, or set it as `defaultTool`.')
    w("")

    w("## CLI commands")
    w("")
    w("| Command | Description |")
    w("| --- | --- |")
    w("| `magent` | Interactive menu. |")
    w("| `magent --go` | Launch + tile, skip menu. |")
    w("| `magent --retile-all` | Re-tile every matching window. |")
    w("| `magent -g <name>` | Launch only projects in a group. |")
    w("| `magent --init` | Re-scan sessions and regenerate config. |")
    w("| `magent --init --base-dir <dir>` | Generate config from a folder of repos. |")
    w("| `magent --edit` | Open config in your default editor. |")
    w("| `magent docs` | Print this reference (pipe to file for AI context). |")
    w("| `magent up` | (Host side) ensure a persistent psmux session per project. |")
    w(
        "| `magent up --json` | Print session status (up/down/projects) as JSON, change nothing. |"
    )
    w("| `magent up -g <group>` | Bring up sessions for only one project group. |")
    w(
        "| `magent attach [host]` | From another PC: bring host sessions up over SSH, tile locally, Alt+V hotkey. |"
    )
    w(
        "| `magent attach <host> -g <group>` | Attach to only one project group on the host. |"
    )
    w(
        "| `magent attach <host> --no-mux` | Attach with a direct SSH window per project (no psmux/tmux). |"
    )
    w(
        "| `magent --attach-to <host>` | (deprecated alias for `magent attach <host>`). |"
    )
    w(
        "| `magent status` | Show which psmux sessions and the upload server are running. |"
    )
    w("| `magent down` | Shut down all running psmux sessions. |")
    w("| `magent down -g <group>` | Shut down only one group's sessions. |")
    w("| `magent down <name> [<name>...]` | Shut down specific sessions by name. |")
    w("| `magent down --all` | Stop every session and the upload server. |")
    w("| `magent serve` | Start upload server for mobile image transfer. |")
    w("| `magent serve -p 9090` | Use a custom port (default 8033). |")
    w("| `magent hotkey` | Listen for Alt+V to upload clipboard images (standalone). |")
    w("| `magent sessions` | List active psmux sessions, pick one to attach. |")
    w("| `magent sessions <name>` | Attach directly to a psmux session by name. |")
    w("| `magent config show` | Display current config. |")
    w("| `magent config layout <cols> <rows>` | Set window grid. |")
    w("| `magent config base-dir <path>` | Set projects folder. |")
    w("| `magent config default-tool <tool>` | Set default AI tool. |")
    w("| `magent config tool <name> <cmd>` | Add/update a tool command. |")
    w("| `magent config remove-tool <name>` | Remove a tool. |")
    w("| `magent config add <path> [-g GROUP] [-t TOOL]` | Add a project. |")
    w("| `magent config remove <path>` | Remove a project. |")
    w("| `magent config enable <path>` | Enable a project. |")
    w("| `magent config disable <path>` | Disable a project. |")
    w("| `magent config set <path> <field> <value>` | Set a project field. |")
    w("| `magent config open` | Open config in editor. |")
    w("| `magent config path` | Print config file path. |")
    w(
        "| `magent config migrate` | Stamp the schema version and backfill project colors. |"
    )
    w("")

    return "\n".join(lines)


@main.command("docs")
def docs_cmd() -> None:
    """Print the full configuration reference (Markdown). Pipe to a file or feed to an AI."""
    click.echo(_generate_docs())
