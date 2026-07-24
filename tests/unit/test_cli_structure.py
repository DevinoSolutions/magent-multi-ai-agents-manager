"""Structural pins for E6 (cli.py -> cli/ package split).

Byte-level --help snapshots (captured from the pre-split baseline), the
command registration sets, an acyclic-import check, and characterization
tests for the two worst-graded functions -- `_config_menu` (radon F/48) and
`_attach_flow` (radon D/29) -- written and green BEFORE their relocation so a
behavior drift during the move is caught immediately. See audit/stage2/E6.md.
"""

from __future__ import annotations

import json
import re
import subprocess

import click
import pytest

from magent import cli


def _normalize_help(output: str) -> str:
    """Click 8.4 brackets the metavar as `[COMMAND]` for `invoke_without_command=True`
    groups (main's own bare --help); click 8.3 renders the same group as `COMMAND`
    (no brackets). This repo's two reachable interpreters resolve different click
    versions (verified: only this one substring differs, on only this one target --
    everything else, incl. every subcommand's help and the full options/commands
    list, is byte-identical across both). Normalizing this one cosmetic bracket
    keeps the pin sensitive to what it exists to catch (a dropped/renamed/
    re-parented command or changed help text) without an environment-dependent
    false failure that has nothing to do with the cli split."""
    return output.replace(
        "[OPTIONS] [COMMAND] [ARGS]...", "[OPTIONS] COMMAND [ARGS]..."
    )


HELP_SNAPSHOTS = {
    (): "Usage: main [OPTIONS] [COMMAND] [ARGS]...\n\n  Open every project in its own terminal and auto-tile across all monitors.\n\nOptions:\n  --go              Skip interactive menu, launch + tile\n  --retile-all      Re-tile every matching window\n  -g, --group TEXT  Launch only projects in this group\n  --init            Re-scan and regenerate config\n  --base-dir PATH   Folder to scan with --init\n  --config PATH     Path to config file\n  --force           With --init, overwrite existing config\n  --edit            Open config in your default editor\n  --attach-to TEXT  Attach to remote psmux sessions (host or user@host)\n  --no-mux          With --attach-to: one plain SSH window per project (no\n                    psmux/tmux)\n  --version         Show the version and exit.\n  --help            Show this message and exit.\n\nCommands:\n  attach     Attach to another machine's magent sessions over SSH.\n  attention  Ambient attention signals for your agent fleet.\n  config     View and modify your magent configuration.\n  docs       Print the full configuration reference (Markdown).\n  doctor     Diagnose the environment: config, env vars, tools, display, dirs.\n  down       Shut down running psmux sessions (and optionally the upload...\n  hotkey     Listen for Alt+V to upload clipboard images to psmux sessions.\n  mobile     Show the phone URL + QR for the image-upload app.\n  serve      Start upload server for mobile image transfer.\n  sessions   List psmux sessions or attach to one.\n  status     Show which psmux sessions and services are currently running.\n  termius    Generate SSH config for Termius — one host that opens all...\n  up         Ensure a persistent psmux session per project (host side of...\n  watch      Live view of every agent session — who needs you, sorted first.\n",
    (
        "attention",
    ): "Usage: main attention [OPTIONS]\n\n  Ambient attention signals for your agent fleet.\n\n  Badges every magent: window title with its session state, flashes the taskbar\n  when an agent needs input or errors, and (when enabled in config) sends a\n  Windows toast and/or an ntfy push. States come from the agent-state store that\n  Claude Code hooks / Codex notify already write.\n\nOptions:\n  -d, --daemon      Run detached\n  --stop            Stop the running daemon\n  --interval FLOAT  Seconds between polls (default: attention.pollIntervalS from\n                    config)\n  --help            Show this message and exit.\n",
    (
        "up",
    ): "Usage: main up [OPTIONS]\n\n  Ensure a persistent psmux session per project (host side of `attach`).\n\nOptions:\n  --json            Print session status as JSON without changing anything\n  --all             Recreate every session, not just the ones that are down\n  -g, --group TEXT  Only projects tagged with this group\n  --help            Show this message and exit.\n",
    (
        "attach",
    ): "Usage: main attach [OPTIONS] [HOST]\n\n  Attach to another machine's magent sessions over SSH.\n\n  HOST is user@host (omit to be prompted; blank uses the host from your local\n  config). Default tiles one window per remote psmux session with Alt+V image\n  paste; --no-mux opens a direct SSH window per project instead. -g limits the\n  flow to one project group on the host; -y skips the bring-up prompt.\n\nOptions:\n  --no-mux          One plain SSH window per project (no psmux/tmux)\n  -g, --group TEXT  Only attach/bring up projects in this group\n  -y, --yes         Skip the bring-up prompt (bring up everything that's down)\n  --help            Show this message and exit.\n",
    (
        "hotkey",
    ): "Usage: main hotkey [OPTIONS]\n\n  Listen for Alt+V to upload clipboard images to psmux sessions.\n\n  Only activates when a 'magent:' titled window is focused. Otherwise the\n  keystroke passes through normally.\n\nOptions:\n  -s, --server TEXT  Upload server URL\n  --help             Show this message and exit.\n",
    (
        "config",
    ): "Usage: main config [OPTIONS] COMMAND [ARGS]...\n\n  View and modify your magent configuration.\n\nOptions:\n  --help  Show this message and exit.\n\nCommands:\n  add           Add a project.\n  base-dir      Set the base directory for project paths.\n  default-tool  Set the default tool for new projects.\n  disable       Disable a project without removing it.\n  enable        Enable a disabled project.\n  layout        Set grid layout.\n  migrate       Migrate the config file to the current schema version,...\n  open          Open config file in your default editor.\n  path          Print the config file path.\n  remove        Remove a project by path (or leaf name).\n  remove-tool   Remove a tool.\n  set           Set a field on a project.\n  show          Display current configuration.\n  tool          Add or update a tool command.\n",
    (
        "watch",
    ): "Usage: main watch [OPTIONS]\n\n  Live view of every agent session — who needs you, sorted first.\n\n  Rows come from the same state store the attention daemon reads (needs-input\n  and errors on top, time-in-state alongside). Press a row number to focus that\n  session's window; q quits.\n\nOptions:\n  --interval FLOAT  Refresh seconds  [default: 2.0]\n  --help            Show this message and exit.\n",
    (
        "docs",
    ): "Usage: main docs [OPTIONS]\n\n  Print the full configuration reference (Markdown). Pipe to a file or feed to\n  an AI.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "doctor",
    ): "Usage: main doctor [OPTIONS]\n\n  Diagnose the environment: config, env vars, tools, display, dirs.\n\n  One line per check with an actionable hint on warn/fail. Exit 0 when nothing\n  failed (warnings allowed), 1 when any check failed.\n\nOptions:\n  --json  Print check results as JSON\n  --help  Show this message and exit.\n",
    (
        "termius",
    ): "Usage: main termius [OPTIONS]\n\n  Generate SSH config for Termius — one host that opens all projects.\n\n  Connects to the 'magent' psmux session with all project windows inside. Switch\n  windows with Ctrl+B then number/name.\n\nOptions:\n  --host TEXT  SSH hostname or IP (default: Tailscale IP)\n  --user TEXT  SSH username (default: current user)\n  --install    Write entry to ~/.ssh/config\n  --help       Show this message and exit.\n",
    (
        "serve",
    ): "Usage: main serve [OPTIONS]\n\n  Start upload server for mobile image transfer.\n\n  Opens a web page on your phone (via Tailscale) where you pick a project,\n  upload an image, and the file path is auto-pasted into that project's Claude\n  session via psmux send-keys.\n\nOptions:\n  -p, --port INTEGER  Port to listen on\n  --host TEXT         Bind a specific address instead of the default (loopback +\n                      Tailscale IP, never the LAN wildcard). Pass 0.0.0.0 to\n                      restore an explicit LAN-wide bind.\n  --ensure            Start the server detached if it isn't already running,\n                      then exit (used by attach).\n  --help              Show this message and exit.\n",
    (
        "mobile",
    ): "Usage: main mobile [OPTIONS]\n\n  Show the phone URL + QR for the image-upload app.\n\n  Scan it once on your phone, then 'Add to Home Screen' to install the uploader\n  as a standalone app -- after that it's one tap to send an image into any\n  magent: session. Run this on the host that serves the uploader.\n\nOptions:\n  -p, --port INTEGER  Upload server port (default: running server, else 8033).\n  --host TEXT         Host/IP for the phone URL (default: Tailscale name or IP).\n  --help              Show this message and exit.\n",
    (
        "sessions",
    ): "Usage: main sessions [OPTIONS] [NAME]\n\n  List psmux sessions or attach to one. Usage: magent sessions [name]\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "status",
    ): "Usage: main status [OPTIONS]\n\n  Show which psmux sessions and services are currently running.\n\nOptions:\n  --json  Print daemon status as JSON\n  --help  Show this message and exit.\n",
    (
        "down",
    ): "Usage: main down [OPTIONS] [NAMES]...\n\n  Shut down running psmux sessions (and optionally the upload server).\n\nOptions:\n  -g, --group TEXT  Only sessions in this group\n  --all             Stop every session, the upload server, and the Alt+V\n                    listener\n  --server          Also stop the upload server\n  --help            Show this message and exit.\n",
    (
        "config",
        "show",
    ): "Usage: main config show [OPTIONS]\n\n  Display current configuration.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "migrate",
    ): "Usage: main config migrate [OPTIONS]\n\n  Migrate the config file to the current schema version, persisting any\n  backfilled colors.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "layout",
    ): "Usage: main config layout [OPTIONS] COLUMNS ROWS\n\n  Set grid layout. Usage: magent config layout 3 2\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "base-dir",
    ): "Usage: main config base-dir [OPTIONS] PATH\n\n  Set the base directory for project paths.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "default-tool",
    ): "Usage: main config default-tool [OPTIONS] TOOL\n\n  Set the default tool for new projects.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "tool",
    ): "Usage: main config tool [OPTIONS] NAME COMMAND\n\n  Add or update a tool command. Usage: magent config tool aider 'aider --model\n  sonnet'\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "remove-tool",
    ): "Usage: main config remove-tool [OPTIONS] NAME\n\n  Remove a tool.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "add",
    ): "Usage: main config add [OPTIONS] PATH\n\n  Add a project. Usage: magent config add ./myapp -g INTERNAL -t claude\n\nOptions:\n  -g, --group TEXT       Group name\n  -t, --tool TEXT        Tool (claude, codex, vscode, ...)\n  -c, --color TEXT       Tab color (#rrggbb)\n  --title TEXT           Custom window title\n  --host TEXT            SSH host for remote projects\n  -w, --windows INTEGER  Number of windows\n  --help                 Show this message and exit.\n",
    (
        "config",
        "remove",
    ): "Usage: main config remove [OPTIONS] PATH\n\n  Remove a project by path (or leaf name).\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "enable",
    ): "Usage: main config enable [OPTIONS] PATH\n\n  Enable a disabled project.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "disable",
    ): "Usage: main config disable [OPTIONS] PATH\n\n  Disable a project without removing it.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "set",
    ): "Usage: main config set [OPTIONS] PATH FIELD VALUE\n\n  Set a field on a project. Usage: magent config set myapp group INTERNAL\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "open",
    ): "Usage: main config open [OPTIONS]\n\n  Open config file in your default editor.\n\nOptions:\n  --help  Show this message and exit.\n",
    (
        "config",
        "path",
    ): "Usage: main config path [OPTIONS]\n\n  Print the config file path.\n\nOptions:\n  --help  Show this message and exit.\n",
}

TOP_LEVEL_COMMANDS = [
    "attach",
    "attention",
    "config",
    "docs",
    "doctor",
    "down",
    "hotkey",
    "mobile",
    "serve",
    "sessions",
    "status",
    "termius",
    "up",
    "watch",
]
CONFIG_SUBCOMMANDS = [
    "add",
    "base-dir",
    "default-tool",
    "disable",
    "enable",
    "layout",
    "migrate",
    "open",
    "path",
    "remove",
    "remove-tool",
    "set",
    "show",
    "tool",
]


def test_help_snapshots(runner):
    """The primary anti-drift net: fails the instant a command's help output
    changes shape, is dropped, renamed, or re-parented during the split."""
    for args, expected in HELP_SNAPSHOTS.items():
        result = runner.invoke(cli.main, [*list(args), "--help"])
        assert result.exit_code == 0
        assert _normalize_help(result.output) == _normalize_help(expected), (
            f"help output drifted for args={args!r}"
        )


def test_version_snapshot(runner):
    result = runner.invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert re.search(r"\d+\.\d+", result.output)


def test_registration_set_top_level():
    assert sorted(cli.main.commands) == TOP_LEVEL_COMMANDS


def test_registration_set_config_subcommands():
    assert sorted(cli.main.commands["config"].commands) == CONFIG_SUBCOMMANDS


def test_acyclic_imports():
    """magent.cli and magent.upload_server must both import standalone --
    upload_server's deferred `_find_config`/`find_config` import and cli's
    command-module registration must never form a load cycle."""
    import magent.cli
    import magent.upload_server  # noqa: F401  # reason: imported for its load side-effect — this is the acyclic-import probe


class TestConfigMenuCharacterization:
    """Pins _config_menu (radon F/48) BEFORE it moves into cli/config_editor.py
    (E6.md Step 0 / Step 8). Scripts click.prompt/click.getchar to: set the
    window grid to 3x2, toggle Happy mobile on, then back out -- and asserts
    the on-disk JSON mutations plus a stable echo substring per choice."""

    def test_scripted_grid_and_happy_toggle(self, tmp_path, monkeypatch, capsys):
        config_file = tmp_path / "magent.config.json"
        config_file.write_text(
            json.dumps(
                {
                    "layout": {"columns": 2, "rows": 1},
                    "settings": {
                        "defaultTool": "claude",
                        "tools": {"claude": "claude --continue"},
                    },
                    "projects": [{"path": "myapp"}],
                }
            )
        )

        responses = iter(["1", "3", "2", "5", "b"])
        monkeypatch.setattr(click, "prompt", lambda *a, **k: next(responses))
        monkeypatch.setattr(click, "getchar", lambda *a, **k: "\n")

        cli._config_menu(config_file)

        data = json.loads(config_file.read_text(encoding="utf-8"))
        assert data["layout"]["columns"] == 3
        assert data["layout"]["rows"] == 2
        assert data["settings"]["happy"] is True

        out = capsys.readouterr().out
        assert "Window grid set to" in out
        assert "Happy mobile" in out and "enabled" in out


class TestAttachFlowCharacterization:
    """Pins _attach_flow (radon D/29) BEFORE it moves into cli/attach.py
    (E6.md Step 0 / Step 10). SSH/subprocess/tiling/hotkey/platform are all
    mocked at the cli module's names -- the psmux path must spawn exactly one
    `wt ... --title magent:<name>` per up-session and hand the titles to
    _tile_titles; the no-host path must prompt and exit 1 when left blank."""

    def test_psmux_path_spawns_and_tiles(self, monkeypatch):
        status = {
            "up": [{"name": "myapp"}],
            "down": [],
            "uploadPort": 8033,
            "projects": [{"name": "myapp", "path": "myapp"}],
        }
        monkeypatch.setattr("magent.cli.attach._ssh_json", lambda *a, **k: status)
        monkeypatch.setattr(
            "magent.cli.attach._ssh_capture", lambda *a, **k: (0, "", "")
        )
        popen_calls = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda args, **k: popen_calls.append(args)
        )
        tiled = []
        monkeypatch.setattr("magent.cli.attach._tile_titles", tiled.append)
        monkeypatch.setattr("magent.cli.attach._maybe_start_hotkey", lambda url: 1234)

        class _FakePlat:
            def supports_hotkey(self) -> bool:
                return True

        monkeypatch.setattr("magent.platform.get_platform", _FakePlat)
        monkeypatch.setattr("time.sleep", lambda s: None)

        cli._attach_flow("user@host", no_mux=False, group=None, yes=False)

        assert len(popen_calls) == 1
        assert "wt" in popen_calls[0]
        assert "--title" in popen_calls[0]
        assert "magent:myapp" in popen_calls[0]
        assert tiled == [["magent:myapp"]]

    def test_no_host_prompts_then_exits(self, monkeypatch):
        monkeypatch.setattr("magent.cli.attach._default_attach_host", lambda: None)
        prompted = []

        def fake_prompt(text, **k):
            prompted.append(text)
            return ""

        monkeypatch.setattr(click, "prompt", fake_prompt)

        with pytest.raises(SystemExit) as exc_info:
            cli._attach_flow(None, no_mux=False, group=None, yes=False)

        assert exc_info.value.code == 1
        assert len(prompted) == 1
