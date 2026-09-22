"""The `magent docs` command: a pure-string Markdown generator for the full
config reference. No I/O beyond stdout via click.echo.

Three hand-written tables (`_PROJECT_FIELD_DOCS`, `_SETTINGS_FIELD_DOCS`,
`_CLI_COMMAND_DOCS`) supply the prose; `_generate_docs` only lays it out.
"""

from __future__ import annotations

import click

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
    (
        "account",
        "string",
        "none",
        (
            "Pin this project to one Claude account id (see `settings.accounts`). "
            "Your intent — magent never writes this field, and honours it even "
            "over the hard usage threshold."
        ),
    ),
    (
        "modelClass",
        "string",
        "inferred",
        (
            "`fable` or `standard`. Which usage cap this project's work counts "
            "against, when magent should not guess. An unknown value is ignored "
            "with a warning."
        ),
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
        (
            "Prefix every window title with `magent:` so the attention daemon's "
            "badges, the Alt+V hotkey, and `magent-name` tiling can recognize magent "
            "windows. Set `false` for bare project-name titles — then title badges, "
            "the Alt+V hotkey, and `project_from_title` no-op, and launch-path tiling "
            "falls back to exact-title matching. `magent attach` windows always keep "
            "the prefix: there the title carries the psmux session id (P3-01), so it "
            "is load-bearing, not cosmetic."
        ),
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
        (
            "Also push toast/ntfy when an agent finishes (enters `done`). Opt-in; "
            "does nothing unless `toast` or `ntfy` is on."
        ),
    ),
    (
        "attention.pollIntervalS",
        "number",
        "`2`",
        (
            "Seconds between daemon polls. Every tick also sweeps the psmux "
            "fleet's process priority and, when `uploadServer` is on, probes the "
            "upload server -- so a shorter interval multiplies real work, not "
            "just repaints. `magent attention --interval` overrides it for one run."
        ),
    ),
    (
        "attention.stalenessWorkingS",
        "number",
        "`1800`",
        (
            "Seconds after which a `working` record stops counting as working. "
            "This is what keeps a session killed mid-turn from showing "
            "'still going...' forever."
        ),
    ),
    (
        "attention.stalenessNeedsInputS",
        "number",
        "`3600`",
        "The same window for `needs-input`.",
    ),
    (
        "attention.debounceS",
        "number",
        "`300`",
        (
            "Minimum seconds between pushes for the same session in the same "
            "state. Gates `toast`/`ntfy` only -- title badges and the taskbar "
            "flash are never debounced."
        ),
    ),
    (
        "attention.stateTtlDays",
        "int",
        "`14`",
        (
            "Days a session's state record survives in `~/.magent/state/` before "
            "the sweep deletes it."
        ),
    ),
    (
        "accounts.enabled",
        "boolean",
        "`false`",
        (
            "Spread projects across your Claude accounts. Off by default: with it "
            "off magent runs no `ccswap` and every pane starts on your default "
            "login, exactly as before this setting existed."
        ),
    ),
    (
        "accounts.softThreshold",
        "number",
        "`85`",
        "Percent of an account's binding usage window at which magent stops placing *new* projects on it (it never evicts one already there).",
    ),
    (
        "accounts.hardThreshold",
        "number",
        "`95`",
        "Percent at which an account is excluded from assignment altogether, and a project on it is offered a move.",
    ),
    (
        "accounts.onLimit",
        "string",
        '`"move-if-reset>2h"`',
        "What to recommend for a session whose account hit its limit: `wait`, `move`, or `move-if-reset>Nh`. An unrecognised value warns and falls back to `wait`.",
    ),
    (
        "accounts.staleAfterS",
        "number",
        "`900`",
        "Seconds after which ccswap's cached usage numbers are reported as stale. A caveat on the table, never a refusal to launch.",
    ),
    (
        "accounts.statusLeft",
        "boolean",
        "`true`",
        "Show the routed account id in each session's psmux status bar.",
    ),
    (
        "accounts.perAccount",
        "object",
        "`{}`",
        'Per-account overrides keyed by ccswap account id: `{"13": {"class": "fable", "exclude": false, "onLimit": "wait"}}`. `class` reserves an account for one model class; `exclude` takes it out of routing.',
    ),
]


# The CLI-commands table: (invocation, what it does). Hand-written on purpose
# -- these descriptions say what a command is FOR and what is surprising about
# it, which a generator reading click's one-line help could not. The price of
# hand-writing is drift, and it was paid: nine commands once shipped with no
# row at all. `tests/unit/test_docs.py` walks the real click registry and fails
# until every registered command has a row here, in both directions.
_CLI_COMMAND_DOCS: list[tuple[str, str]] = [
    ("magent", "Interactive menu."),
    ("magent --go", "Launch + tile, skip menu."),
    ("magent --retile-all", "Re-tile every matching window."),
    ("magent -g <name>", "Launch only projects in a group."),
    (
        "magent --init",
        (
            "Re-scan sessions and regenerate config. Refuses with exit 1 if a "
            "config already exists; `--force` overwrites it."
        ),
    ),
    ("magent --init --base-dir <dir>", "Generate config from a folder of repos."),
    ("magent --edit", "Open config in your default editor."),
    ("magent docs", "Print this reference (pipe to file for AI context)."),
    (
        "magent up",
        (
            "(Host side) ensure a persistent psmux session per project, and start "
            "the upload server when `settings.uploadServer` is on. On Windows an "
            "SSH login lands in logon Session 0, which has no desktop -- there this "
            "re-runs itself on yours instead, per `MAGENT_SESSION0_POLICY`."
        ),
    ),
    (
        "magent up --json",
        (
            "Print session status (up/down/projects) as JSON. Creates nothing -- "
            "this is the read `magent attach` polls over SSH -- though it does "
            "refresh each live session's psmux status line."
        ),
    ),
    ("magent up -g <group>", "Bring up sessions for only one project group."),
    (
        "magent attach [host]",
        (
            "From another PC: bring host sessions up over SSH, tile locally, Alt+V "
            "hotkey. Each psmux pane is supervised and reattaches itself once the "
            "host is reachable again -- except with `--no-reconnect` (a plain "
            "one-shot ssh pane), with `--no-mux` (its direct-ssh windows are never "
            "supervised), or when `magent-attach-client` is not on PATH (every pane "
            "degrades to bare ssh, with one warning)."
        ),
    ),
    (
        "magent attach <host> -g <group>",
        "Attach to only one project group on the host.",
    ),
    (
        "magent attach <host> --no-mux",
        "Attach with a direct SSH window per project (no psmux/tmux).",
    ),
    ("magent --attach-to <host>", "(deprecated alias for `magent attach <host>`)."),
    (
        "magent status",
        (
            "Show which psmux sessions, the upload server, the Alt+V listener and "
            "the attention daemon are running, plus a count of agents waiting on "
            "you. Exit 0 healthy, 1 no readable config, 3 degraded (a dead upload "
            "server, a stale or dead listener, or a stale/crashed attention daemon)."
        ),
    ),
    (
        "magent doctor",
        (
            "Check the environment one line at a time -- config, env vars, tools, "
            "monitors, Alt+V, psmux -- most with a repair hint when they warn or "
            "fail. Exits 1 only if a check FAILED; warnings still pass. "
            "`--json` for the same as data, including monitor topology."
        ),
    ),
    (
        "magent watch",
        (
            "Live table of every session in the agent-state store, whoever needs you "
            "first -- so a fleet with no lifecycle hooks wired shows nothing (see "
            "`magent hooks install`). Keys 1-9 focus that window, `q` quits; on "
            "POSIX the key needs Enter after it."
        ),
    ),
    (
        "magent attention",
        (
            "Ambient signals for the whole fleet: badge each `magent:` title with "
            "its session state, flash the taskbar on needs-input/error, and (per "
            "`settings.attention`) toast or ntfy. Every poll also sweeps the psmux "
            "fleet back to above-normal priority, and revives `magent serve` when "
            "`settings.uploadServer` is on and `MAGENT_UPLOAD_SUPERVISOR` has not "
            "turned the supervisor off."
        ),
    ),
    (
        "magent attention -d",
        (
            "The same, detached, one per machine. Its heartbeat shows in `magent "
            "status`; `--stop` ends it, and so does `down --all`."
        ),
    ),
    (
        "magent hooks install",
        (
            "Wire magent's state hook into Claude Code's lifecycle events, so "
            "session states are reported by the agent rather than scraped off a "
            "screen. Idempotent; prints the Codex `notify` recipe to paste "
            "yourself."
        ),
    ),
    (
        "magent hooks status",
        "Show which lifecycle events are wired and how fresh the state store is.",
    ),
    (
        "magent down",
        (
            "Shut down every psmux session this config lists in scope -- "
            "deliberately not just the ones a liveness probe happens to see -- then "
            "re-probe and report only what it PROVED stopped, naming any survivor. "
            "Falls back to the host you last attached to when none are local."
        ),
    ),
    ("magent down -g <group>", "Shut down only one group's sessions."),
    ("magent down <name> [<name>...]", "Shut down specific sessions by name."),
    (
        "magent down --all",
        (
            "Stop every session, the upload server, the Alt+V listener and the "
            "attention daemon. The broadest shutdown there is -- a plain `down` "
            "leaves all three of those daemons running."
        ),
    ),
    (
        "magent down --host <user@host>",
        (
            "Run the same shutdown on that machine over SSH, closing the local "
            "attach windows first."
        ),
    ),
    (
        "magent serve",
        (
            "Start the upload server for mobile image transfer. Binds loopback plus "
            "this machine's Tailscale IP, and never the LAN wildcard: there is no "
            "auth token, so that bind IS the access control. Phone upload stays "
            "dark until Tailscale is up."
        ),
    ),
    (
        "magent serve -p 9090",
        "Use a custom port (default: this config's `uploadPort`, else 8033).",
    ),
    (
        "magent serve --host 0.0.0.0",
        (
            "Deliberately bind LAN-wide instead. The escape hatch, not a default -- "
            "see the bind note above before using it."
        ),
    ),
    (
        "magent mobile",
        (
            "Print the uploader's phone URL and a QR for it, so you can scan once "
            "and Add to Home Screen. Run it on the machine that serves the uploader."
        ),
    ),
    (
        "magent termius",
        (
            "Print an SSH config block for a single `magent` host that opens the "
            "session picker on connect. `--install` writes it into `~/.ssh/config` "
            "between markers, replacing its own previous block."
        ),
    ),
    ("magent hotkey", "Listen for Alt+V to upload clipboard images (standalone)."),
    (
        "magent terminal install",
        "Bind Ctrl+Backspace / Shift+Enter in Windows Terminal so they survive psmux.",
    ),
    (
        "magent terminal status",
        "Report whether those keybindings are installed, conflicting or missing.",
    ),
    ("magent sessions", "List active psmux sessions, pick one to attach."),
    ("magent sessions <name>", "Attach directly to a psmux session by name."),
    (
        "magent sessions --json",
        (
            "Print every configured session as JSON -- `name`, `cwd`, `live`, "
            "`state`, `model`, `effort`, `account` -- and exit. The read surface "
            "for scripts; `account` is null when a session is unrouted."
        ),
    ),
    (
        "magent send <session> <text>",
        (
            "Paste a prompt into one running agent and press Enter, then confirm it "
            "left the input line. SESSION matches case-insensitively (exact, then a "
            "unique prefix/substring). Newlines are collapsed to spaces -- `--file` "
            "bodies included -- because a lone Enter would submit the prompt early, "
            "one line at a time. `--wait-idle` holds until the agent is between "
            "turns and `--compact` runs `/compact` first, both bounded by "
            "`--timeout` (180s). Exit 2 no such live session, 3 psmux error, 4 the "
            "send went unconfirmed or the session never went idle."
        ),
    ),
    (
        "magent model <session> <model>",
        (
            "Switch a session's model, only while it is idle -- never mid-turn -- "
            "then re-read the pane footer to verify the switch took. `--effort` "
            "sets reasoning effort too; `--all` does every live session. A busy "
            "session is retried until `--max-minutes` runs out, and abandoned after "
            "3 failed attempts. Exit 4 if any session's switch went unconfirmed."
        ),
    ),
    (
        "magent peek <session>",
        (
            "Print the last lines of a session's pane, a read-only glance (`-n` for "
            "how many). Safe to redirect: glyphs this console cannot encode become "
            "`?` rather than crashing."
        ),
    ),
    (
        "magent account",
        (
            "Show every Claude account ccswap reports, its usage, and which "
            "projects sit on it."
        ),
    ),
    (
        "magent account plan",
        "Show which account each project WOULD get, and why. Changes nothing.",
    ),
    (
        "magent account pin <project> <account>",
        (
            "Record in this config that PROJECT belongs on that account; the pin "
            "wins over every usage threshold. It applies at that project's next "
            "launch and never moves a running session, and it only routes anything "
            "while routing is enabled (`settings.accounts.enabled`, off by "
            "default) -- the pin is written either way, so you can set it up first."
        ),
    ),
    ("magent account unpin <project>", "Remove the pin."),
    ("magent account refresh", "Ask ccswap for fresher usage numbers."),
    ("magent config show", "Display current config."),
    ("magent config layout <cols> <rows>", "Set window grid."),
    ("magent config base-dir <path>", "Set projects folder."),
    ("magent config default-tool <tool>", "Set default AI tool."),
    ("magent config tool <name> <cmd>", "Add/update a tool command."),
    ("magent config remove-tool <name>", "Remove a tool."),
    ("magent config add <path> [-g GROUP] [-t TOOL]", "Add a project."),
    ("magent config remove <path>", "Remove a project."),
    ("magent config enable <path>", "Enable a project."),
    ("magent config disable <path>", "Disable a project."),
    ("magent config set <path> <field> <value>", "Set a project field."),
    ("magent config open", "Open config in editor."),
    ("magent config path", "Print config file path."),
    (
        "magent config migrate",
        "Stamp the schema version and backfill project colors.",
    ),
    (
        "magent config edit [host]",
        (
            "Edit ANOTHER machine's config in your editor over SSH (omit the host "
            "to reuse your last attach target)."
        ),
    ),
    (
        "magent config cat",
        "Print this machine's raw config to stdout (host side of `config edit`).",
    ),
    (
        "magent config put",
        (
            "Replace this machine's config from stdin, validated, with a backup "
            "(host side of `config edit`)."
        ),
    ),
]


def _generate_docs() -> str:
    # deferred: resolving __version__ costs an importlib.metadata import, and
    # only this generator needs it -- every other command shouldn't pay it.
    from magent import __version__

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
    for command, desc in _CLI_COMMAND_DOCS:
        w(f"| `{command}` | {desc} |")
    w("")

    return "\n".join(lines)


@main.command("docs")
def docs_cmd() -> None:
    """Print the full configuration reference (Markdown). Pipe to a file or feed to an AI."""
    click.echo(_generate_docs())
